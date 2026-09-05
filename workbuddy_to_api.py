#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workbuddy_to_api - OpenAI / Anthropic compatible API for WorkBuddy.

Python 3.10+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import http.client
import json
import math
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

VERSION = "2.0.0"
APP_NAME = "workbuddy_to_api"
ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / "runtime"
STATE_FILE = Path(os.getenv("WORKBUDDY_PROXY_STATE_FILE") or (RUNTIME_DIR / "proxy-state.json")).expanduser().resolve()
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

FALLBACK_MODELS = [
    "auto", "glm-5v-turbo", "glm-5.1", "glm-5.0-turbo", "glm-5.0",
    "glm-4.7", "kimi-k2.5", "minimax-m2.7", "deepseek-v3-2-volc",
]
DEFAULT_ALIASES = {
    "workbuddy": "auto", "workbuddy-auto": "auto", "codebuddy": "auto",
    "gpt-4o": "auto", "gpt-4o-mini": "auto",
    "claude-3-7-sonnet": "auto", "claude-3-7-sonnet-latest": "auto",
    "claude-3-7-sonnet-20250219": "auto", "claude-3.7-sonnet": "auto",
    "claude-4-sonnet": "auto", "claude-sonnet-4": "auto",
    "claude-sonnet-4-0": "auto", "claude-sonnet-4-20250514": "auto",
}
MCP_PROTOCOL_VERSION = "2025-06-18"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(level: str, message: str, details: Any = None) -> None:
    suffix = ""
    if details is not None:
        suffix = " " + (details if isinstance(details, str) else json.dumps(details, ensure_ascii=False, separators=(",", ":")))
    print(f"{now_iso()} [{level}] {message}{suffix}", flush=True)


def env_int(name: str, fallback: int) -> int:
    try:
        value = int(os.getenv(name, ""))
        return value if value > 0 else fallback
    except ValueError:
        return fallback


def env_bool(name: str, fallback: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return fallback
    return value.strip().lower() not in {"0", "false", "no", "off"}


def split_csv(value: str | None, fallback: Optional[list[str]] = None) -> list[str]:
    items = [x.strip() for x in (value or "").split(",") if x.strip()]
    return list(dict.fromkeys(items)) if items else list(fallback or [])


def split_paths(value: str | None) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in (value or "").split(";") if x.strip()))


def slug(value: str) -> str:
    return (re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value))[:60] or "auto")


def uid(prefix: str = "") -> str:
    value = uuid.uuid4().hex
    return f"{prefix}{value}" if prefix else value


def load_dotenv(filename: Path) -> None:
    if not filename.exists():
        return
    try:
        for raw in filename.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except Exception as exc:
        log("config", f".env load error: {exc}")


def read_json_file(filename: Path) -> Any:
    try:
        return json.loads(filename.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_loopback_host(host: str) -> bool:
    return host.lower() in {"127.0.0.1", "localhost", "::1"}


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def estimate_tokens(text: Any) -> int:
    value = str(text or "")
    if not value:
        return 0
    ascii_count = sum(1 for c in value if ord(c) < 128)
    non_ascii = len(value) - ascii_count
    return max(1, math.ceil(ascii_count / 4) + math.ceil(non_ascii * 0.9))


def usage_for(prompt: str, output: str) -> dict[str, int]:
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(output)
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}


class ProxyError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


def parse_aliases(value: str | None) -> dict[str, str]:
    aliases = dict(DEFAULT_ALIASES)
    if value:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                aliases.update({str(k): str(v) for k, v in parsed.items()})
        except Exception as exc:
            log("config", f"WORKBUDDY_MODEL_ALIASES JSON error: {exc}")
    return aliases


def extract_quoted_flag(command_line: str, flag: str) -> str:
    match = re.search(re.escape(flag) + r'\s+"((?:\\.|[^"\\])*)"', command_line or "", re.I)
    if not match:
        return ""
    return match.group(1).replace(r'\"', '"').replace(r"\\", "\\")


def discover_runtime_mcp_config() -> Optional[dict[str, Any]]:
    if os.name != "nt":
        return None
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'WorkBuddy.exe' -and $_.CommandLine -match '--mcp-config' } | "
        "Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = result.stdout.strip().lstrip("\ufeff")
        if not raw:
            return None
        parsed = json.loads(raw)
        command_lines = parsed if isinstance(parsed, list) else [parsed]
        for command_line in command_lines:
            value = extract_quoted_flag(str(command_line), "--mcp-config")
            if not value.startswith("{"):
                continue
            try:
                candidate = json.loads(value)
                servers = candidate.get("mcpServers", {}) if isinstance(candidate, dict) else {}
                for server in servers.values():
                    if not isinstance(server, dict):
                        continue
                    url = str(server.get("url", ""))
                    headers = server.get("headers", {})
                    if re.match(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::|/)", url, re.I) and isinstance(headers, dict) and isinstance(headers.get("Authorization"), str):
                        return candidate
            except Exception:
                pass
    except Exception:
        pass
    return None


def resolve_mcp_configuration(explicit: str, user_config_path: Path) -> tuple[str, str, dict[str, str]]:
    if explicit.strip():
        return explicit.strip(), "explicit", {}
    user_config = read_json_file(user_config_path) if user_config_path.exists() else None
    runtime_config = discover_runtime_mcp_config()
    sources: dict[str, str] = {}
    if isinstance(user_config, dict):
        for name in user_config.get("mcpServers", {}):
            sources[str(name)] = "user-file"
    if isinstance(runtime_config, dict):
        for name in runtime_config.get("mcpServers", {}):
            sources[str(name)] = "workbuddy-runtime"
        merged = dict(user_config) if isinstance(user_config, dict) else {}
        merged.update(runtime_config)
        merged["mcpServers"] = {
            **(user_config.get("mcpServers", {}) if isinstance(user_config, dict) else {}),
            **runtime_config.get("mcpServers", {}),
        }
        return json.dumps(merged, ensure_ascii=False, separators=(",", ":")), ("workbuddy-runtime+user" if user_config else "workbuddy-runtime"), sources
    if isinstance(user_config, dict):
        return str(user_config_path), "user-file", sources
    return "", "none", {}


def normalize_catalog_model(model: Any) -> Optional[dict[str, Any]]:
    if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip():
        return None
    model_id = model["id"].strip()
    tags = [x for x in model.get("tags", []) if isinstance(x, str)]
    model_type = "chat"
    if any("image" in x for x in tags): model_type = "image"
    if any("video" in x for x in tags): model_type = "video"
    if model.get("supportsToolCall") is not True and re.search(r"completion|rewrite|jump|nes", model_id, re.I): model_type = "internal"
    return {
        "id": model_id, "name": model.get("name") if isinstance(model.get("name"), str) else model_id,
        "vendor": model.get("vendor") if isinstance(model.get("vendor"), str) else None,
        "type": model_type, "tags": tags,
        "credits": model.get("credits") if isinstance(model.get("credits"), str) else None,
        "maxInputTokens": model.get("maxInputTokens") if isinstance(model.get("maxInputTokens"), (int, float)) else None,
        "maxOutputTokens": model.get("maxOutputTokens") if isinstance(model.get("maxOutputTokens"), (int, float)) else None,
        "supportsToolCall": model.get("supportsToolCall") is True,
        "supportsImages": model.get("supportsImages") is True,
        "supportsReasoning": model.get("supportsReasoning") is True,
        "onlyReasoning": model.get("onlyReasoning") is True,
        "descriptionZh": model.get("descriptionZh") if isinstance(model.get("descriptionZh"), str) else None,
        "descriptionEn": model.get("descriptionEn") if isinstance(model.get("descriptionEn"), str) else None,
    }


def normalize_catalog(models: Any, cli_models: Any, source: str, updated_at: Optional[str] = None) -> dict[str, Any]:
    result, seen = [], set()
    for raw in models if isinstance(models, list) else []:
        item = normalize_catalog_model(raw)
        if item and item["id"] not in seen:
            seen.add(item["id"]); result.append(item)
    cli = list(dict.fromkeys(x.strip() for x in (cli_models if isinstance(cli_models, list) else []) if isinstance(x, str) and x.strip()))
    return {"models": result, "cliModels": cli, "source": source, "updatedAt": updated_at}


def load_cached_catalog(home: Path) -> Optional[dict[str, Any]]:
    directory = home / ".workbuddy" / "local_storage"
    if not directory.exists():
        return None
    best = None
    for filename in directory.glob("entry_*.info"):
        try:
            parsed = json.loads(filename.read_text(encoding="utf-8-sig"))
            entries = parsed if isinstance(parsed, list) else [parsed]
            for entry in entries:
                data = entry.get("data") if isinstance(entry, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get("models"), list) or not data["models"]:
                    continue
                cli_agent = next((x for x in data.get("agents", []) if isinstance(x, dict) and x.get("name") == "cli"), None)
                stamp = float(entry.get("ts") or filename.stat().st_mtime * 1000)
                if best is None or stamp > best[0]:
                    best = (stamp, data, cli_agent.get("models", []) if cli_agent else [], filename)
        except Exception as exc:
            log("catalog", f"skipped cache {filename}: {exc}")
    if not best:
        return None
    stamp, data, cli, filename = best
    updated = _dt.datetime.fromtimestamp(stamp / 1000, _dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return normalize_catalog(data.get("models", []), cli, f"workbuddy-cache:{filename.name}", updated)


def load_bundled_catalog(cli_script: Path) -> Optional[dict[str, Any]]:
    product_path = Path(os.getenv("WORKBUDDY_PRODUCT_CONFIG", "")) if os.getenv("WORKBUDDY_PRODUCT_CONFIG") else cli_script.parent.parent / "product.json"
    parsed = read_json_file(product_path)
    if not isinstance(parsed, dict):
        return None
    cli_agent = next((x for x in parsed.get("agents", []) if isinstance(x, dict) and x.get("name") == "cli"), None)
    return normalize_catalog(parsed.get("models", []), cli_agent.get("models", []) if cli_agent else [], "workbuddy-bundled-product")


def load_model_catalog(cli_script: Path, home: Path) -> dict[str, Any]:
    cached = load_cached_catalog(home)
    if cached and cached["models"]: return cached
    bundled = load_bundled_catalog(cli_script)
    if bundled and bundled["models"]: return bundled
    return normalize_catalog([{"id": x, "name": x, "supportsToolCall": True} for x in FALLBACK_MODELS], FALLBACK_MODELS, "built-in-fallback")


def is_object(value: Any) -> bool:
    return isinstance(value, dict)


def normalize_client_tools(body: dict[str, Any], protocol: str) -> list[dict[str, Any]]:
    tools = []
    for entry in body.get("tools", []) if isinstance(body.get("tools"), list) else []:
        if not isinstance(entry, dict): continue
        name, description, schema = "", "", None
        if protocol == "anthropic":
            name = str(entry.get("name", "")).strip(); description = str(entry.get("description", "")); schema = entry.get("input_schema")
        elif protocol == "responses":
            fn = entry.get("function") if isinstance(entry.get("function"), dict) else entry
            name = str(fn.get("name", "")).strip(); description = str(fn.get("description", "")); schema = fn.get("parameters") or fn.get("input_schema")
        else:
            fn = entry.get("function") if isinstance(entry.get("function"), dict) else None
            if entry.get("type") and entry.get("type") != "function" and not fn: continue
            source = fn or entry
            name = str(source.get("name", "")).strip(); description = str(source.get("description", "")); schema = source.get("parameters") or source.get("input_schema")
        if name:
            tools.append({"name": name, "description": description, "input_schema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}}})
    return tools


def normalize_tool_choice(body: dict[str, Any], protocol: str) -> dict[str, Any]:
    value = body.get("tool_choice")
    mode, name = "auto", None
    if isinstance(value, str):
        if value == "none": mode = "none"
        elif value in {"required", "any"}: mode = "required"
    elif isinstance(value, dict):
        typ = str(value.get("type", "")).strip()
        fn = value.get("function") if isinstance(value.get("function"), dict) else {}
        name = str(value.get("name") or fn.get("name") or "").strip() or None
        if typ == "none": mode = "none"
        elif typ in {"required", "any"}: mode = "required"
        elif typ in {"tool", "function"} or name: mode = "specific" if name else "required"
    parallel = (value.get("disable_parallel_tool_use") is not True) if protocol == "anthropic" and isinstance(value, dict) else body.get("parallel_tool_calls") is not False
    return {"mode": mode, "name": name, "parallel": parallel}


def build_client_tool_prompt(base_prompt: str, tools: list[dict[str, Any]], choice: dict[str, Any]) -> str:
    if choice["mode"] == "specific": mode = f'You must call the tool named "{choice["name"]}".'
    elif choice["mode"] == "required": mode = "You must call at least one declared client tool."
    else: mode = "Call a declared client tool when needed; otherwise return a final answer."
    parallel = "You may return multiple independent calls in the calls array." if choice["parallel"] else "Return at most one call in the calls array."
    return f'''{base_prompt}

CLIENT-SIDE TOOL SELECTION PROTOCOL
The tools below are executed by the API client. Do not execute these client tools as internal WorkBuddy tools.
Return exactly one JSON object and no markdown fence.
For tool calls:
{{"type":"tool_calls","commentary":"OPTIONAL_PROGRESS_TEXT","calls":[{{"name":"TOOL_NAME","arguments":{{}}}}]}}
For a final response:
{{"type":"final","content":"FINAL_TEXT"}}
{mode}
{parallel}
Use only declared tool names. Arguments must be a JSON object matching the declared input schema.
Never return a progress update by itself or label future work as a final response. If you say that you will inspect, read, search, run, edit, or verify something, include the required tool call in the same tool_calls object.
The complete tool-call envelope must be valid JSON: escape every double quote inside a string value and encode line breaks as \\n. In particular, the `input` string for the exec tool contains JavaScript and must not contain unescaped quotes.
If the conversation already contains tool results, use those results and return a final response unless another declared call is needed.

Declared client tools:
{json.dumps(tools, ensure_ascii=False, separators=(",", ":"))}'''


def parse_json_object(raw: Any) -> Optional[dict[str, Any]]:
    text = str(raw or "").strip(); candidates = [text]
    fence = re.match(r"^\s*```(?:json)?\s*([\s\S]*?)\s*```\s*$", text, re.I)
    if fence: candidates.append(fence.group(1).strip())
    first, last = text.find("{"), text.rfind("}")
    if first >= 0 and last > first: candidates.append(text[first:last + 1])
    for candidate in dict.fromkeys(candidates):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict): return value
        except Exception: pass
    return None


def normalize_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict): return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict): return parsed
        except Exception: pass
        return {"value": value}
    if value is None: return {}
    return {"value": value}


def decode_relaxed_json_string(value: str) -> str:
    """Decode common JSON escapes without rejecting an otherwise recoverable tool payload."""
    result: list[str] = []; index = 0
    escapes = {"\\\"": "\"", "\\\\": "\\", "\\/": "/", "\\b": "\\b", "\\f": "\\f", "\\n": "\\n", "\\r": "\\r", "\\t": "\\t"}
    while index < len(value):
        if value[index] != "\\" or index + 1 >= len(value):
            result.append(value[index]); index += 1; continue
        token = value[index:index + 2]
        if token == "\\u" and index + 6 <= len(value):
            try:
                result.append(chr(int(value[index + 2:index + 6], 16))); index += 6; continue
            except ValueError: pass
        result.append(escapes.get(token, value[index + 1])); index += 2
    return "".join(result)


def normalize_exec_input(value: str) -> str:
    """Turn double-escaped line breaks back into source newlines for client exec calls."""
    if "\n" in value or "\\n" not in value:
        return value
    if not re.search(r"//\s*@exec:|\b(?:const|let|var|for|await)\b|tools\.exec_command", value):
        return value
    return value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")


def normalize_tool_arguments(name: str, value: Any) -> dict[str, Any]:
    arguments = normalize_arguments(value)
    if name == "exec" and isinstance(arguments.get("input"), str):
        arguments = dict(arguments)
        arguments["input"] = normalize_exec_input(arguments["input"])
    return arguments


def parse_relaxed_exec_output(raw: str, tools: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Recover one common malformed exec envelope emitted by WorkBuddy."""
    if not re.search(r'"type"\s*:\s*"tool_calls"', raw): return None
    text = raw.strip(); first, last = text.find("{"), text.rfind("}")
    if first < 0 or last <= first: return None
    text = text[first:last + 1]
    allowed = {x["name"] for x in tools}
    name_match = re.search(r'"name"\s*:\s*"([^"\\]+)"\s*,\s*"arguments"\s*:', text)
    if not name_match or name_match.group(1) not in allowed: return None
    input_match = re.search(
        r'"arguments"\s*:\s*\{\s*"input"\s*:\s*"([\s\S]*)"\s*'
        r'(?:\}\s*){1,2}\]\s*\}(?:\s*\]\s*\})?\s*$',
        text,
    )
    if not input_match: return None
    call_id = None
    id_match = re.search(r'"id"\s*:\s*"([^"\\]+)"', text[:name_match.start()])
    if id_match: call_id = id_match.group(1)
    name = name_match.group(1)
    arguments = normalize_tool_arguments(name, {"input": decode_relaxed_json_string(input_match.group(1))})
    return {"type": "tool_calls", "calls": [{"id": call_id, "name": name, "arguments": arguments}], "commentary": ""}


def parse_client_tool_output(raw: str, tools: list[dict[str, Any]], choice: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_json_object(raw); allowed = {x["name"] for x in tools}; calls = []
    if parsed is None:
        recovered = parse_relaxed_exec_output(raw, tools)
        if recovered is not None: return recovered
    source = []
    if parsed:
        if isinstance(parsed.get("calls"), list): source = parsed["calls"]
        elif isinstance(parsed.get("tool_calls"), list): source = parsed["tool_calls"]
        elif parsed.get("type") in {"tool_use", "function_call"}: source = [parsed]
    for entry in source:
        if not isinstance(entry, dict): continue
        fn = entry.get("function") if isinstance(entry.get("function"), dict) else entry
        name = str(fn.get("name") or entry.get("name") or "").strip()
        if name not in allowed: continue
        args = fn.get("arguments", fn.get("input", entry.get("arguments", entry.get("input", {}))))
        calls.append({"id": str(entry.get("id") or "").strip() or None, "name": name, "arguments": normalize_tool_arguments(name, args)})
    if not choice["parallel"]: calls = calls[:1]
    if choice["mode"] == "specific": calls = [x for x in calls if x["name"] == choice["name"]]
    if not calls and parsed and (choice["mode"] == "specific" or len(tools) == 1):
        inferred = choice.get("name") or tools[0]["name"]
        looks_final = parsed.get("type") in {"final", "answer", "message"} or isinstance(parsed.get("content"), str) or isinstance(parsed.get("answer"), str)
        if inferred in allowed and not looks_final:
            if isinstance(parsed.get("arguments"), dict): args = parsed["arguments"]
            elif isinstance(parsed.get("input"), dict): args = parsed["input"]
            else: args = {k: v for k, v in parsed.items() if k not in {"type", "content", "text", "answer", "calls", "tool_calls", "name"}}
            calls = [{"id": None, "name": inferred, "arguments": normalize_tool_arguments(inferred, args)}]
    if calls:
        commentary = parsed.get("commentary", parsed.get("content")) if parsed else None
        return {"type": "tool_calls", "calls": calls, "commentary": commentary if isinstance(commentary, str) else ""}
    if parsed and parsed.get("type") == "final":
        content = parsed.get("content")
        if isinstance(content, str) and choice["mode"] not in {"required", "specific"}:
            return {"type": "final", "content": content}
        reason = "tool_choice requires a tool call" if choice["mode"] in {"required", "specific"} else "final content must be a string"
        return {"type": "invalid", "reason": reason}
    if parsed is None:
        return {"type": "invalid", "reason": "response is not a valid protocol JSON object"}
    return {"type": "invalid", "reason": "response is neither a valid tool call nor a final response"}


def make_tool_plan(body: dict[str, Any], protocol: str, base_prompt: str) -> dict[str, Any]:
    tools = normalize_client_tools(body, protocol); choice = normalize_tool_choice(body, protocol)
    enabled = bool(tools) and choice["mode"] != "none"
    return {"tools": tools, "choice": choice, "enabled": enabled, "profile": "protocol" if enabled else "agent", "prompt": build_client_tool_prompt(base_prompt, tools, choice) if enabled else base_prompt}


def build_protocol_repair_prompt(tools: list[dict[str, Any]], choice: dict[str, Any]) -> str:
    return build_client_tool_prompt(
        "Your previous response did not follow the client-side tool protocol. Continue the same request and retry now. Do not repeat a progress update without its tool call.",
        tools,
        choice,
    )


def assign_call_ids(calls: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [{**x, "id": x.get("id") or f"{prefix}_{uuid.uuid4().hex}"} for x in calls]



@dataclass
class Config:
    host: str
    port: int
    api_key: str
    workbuddy_exe: Path
    cli_script: Path
    product_config: Path
    account_session_path: Path
    account_timeout_ms: int
    usage_ledger_max_records: int
    cwd: Path
    default_model: str
    models: list[str]
    model_catalog: dict[str, dict[str, Any]]
    model_catalog_source: str
    model_catalog_updated_at: Optional[str]
    cli_models: list[str]
    aliases: dict[str, str]
    disable_tools: bool
    tools: str
    allowed_tools: list[str]
    disallowed_tools: list[str]
    add_dirs: list[str]
    permission_mode: str
    mcp_config: str
    mcp_config_source: str
    mcp_server_sources: dict[str, str]
    strict_mcp_config: bool
    mcp_refresh_ms: int
    mcp_admin_timeout_ms: int
    mcp_admin_cache_ms: int
    event_max_bytes: int
    max_turns: int
    request_timeout_ms: int
    sse_heartbeat_ms: int
    start_timeout_ms: int
    system_prompt: str
    protocol_system_prompt: str
    configured_model_ids: list[str] = field(default_factory=list)
    last_catalog_refresh_ms: float = 0

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        local = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        default_exe = local / "Programs" / "WorkBuddy" / "WorkBuddy.exe"
        packed_cli = local / "Programs" / "WorkBuddy" / "resources" / "app.asar" / "cli" / "bin" / "codebuddy"
        unpacked_cli = local / "Programs" / "WorkBuddy" / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"
        default_cli = unpacked_cli if unpacked_cli.exists() else packed_cli
        workbuddy_exe = Path(args.workbuddy_exe or os.getenv("WORKBUDDY_EXE") or default_exe).expanduser().resolve()
        cli_script = Path(args.cli_script or os.getenv("WORKBUDDY_CLI_SCRIPT") or default_cli).expanduser().resolve()
        default_product_config = cli_script.parent.parent / "product.json"
        product_config = Path(os.getenv("WORKBUDDY_PRODUCT_CONFIG") or default_product_config).expanduser().resolve()
        default_account_session = local / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
        account_session_path = Path(os.getenv("WORKBUDDY_ACCOUNT_SESSION_PATH") or default_account_session).expanduser().resolve()
        catalog = load_model_catalog(cli_script, Path.home())
        configured = split_csv(os.getenv("WORKBUDDY_MODELS"), [])
        available = catalog["cliModels"] or [x["id"] for x in catalog["models"]]
        ids = configured or available
        default_model = args.model or os.getenv("WORKBUDDY_DEFAULT_MODEL") or "auto"
        if default_model not in ids: ids.insert(0, default_model)
        aliases = parse_aliases(os.getenv("WORKBUDDY_MODEL_ALIASES"))
        user_mcp = Path.home() / ".workbuddy" / ".mcp.json"
        explicit_mcp = args.mcp_config if args.mcp_config is not None else os.getenv("WORKBUDDY_MCP_CONFIG", "")
        mcp_value, mcp_source, mcp_sources = resolve_mcp_configuration(explicit_mcp or "", user_mcp)
        return cls(
            host=args.host or os.getenv("PROXY_HOST") or "127.0.0.1",
            port=args.port or env_int("PROXY_PORT", 3000),
            api_key=args.api_key if args.api_key is not None else os.getenv("PROXY_API_KEY", ""),
            workbuddy_exe=workbuddy_exe, cli_script=cli_script, product_config=product_config,
            account_session_path=account_session_path,
            account_timeout_ms=env_int("WORKBUDDY_ACCOUNT_TIMEOUT_MS", 15000),
            usage_ledger_max_records=env_int("WORKBUDDY_USAGE_LEDGER_MAX_RECORDS", 1000),
            cwd=Path(args.cwd or os.getenv("WORKBUDDY_CWD") or Path.cwd()).expanduser().resolve(),
            default_model=default_model, models=list(dict.fromkeys(ids)),
            model_catalog={x["id"]: x for x in catalog["models"]},
            model_catalog_source="WORKBUDDY_MODELS" if configured else catalog["source"],
            model_catalog_updated_at=catalog.get("updatedAt"), cli_models=catalog["cliModels"], aliases=aliases,
            disable_tools=args.disable_tools or env_bool("WORKBUDDY_DISABLE_TOOLS", False),
            tools=args.tools or os.getenv("WORKBUDDY_TOOLS") or "default",
            allowed_tools=split_csv(os.getenv("WORKBUDDY_ALLOWED_TOOLS"), []),
            disallowed_tools=split_csv(os.getenv("WORKBUDDY_DISALLOWED_TOOLS"), []),
            add_dirs=split_paths(os.getenv("WORKBUDDY_ADD_DIRS")),
            permission_mode=args.permission_mode or os.getenv("WORKBUDDY_PERMISSION_MODE") or "bypassPermissions",
            mcp_config=mcp_value, mcp_config_source=mcp_source, mcp_server_sources=mcp_sources,
            strict_mcp_config=env_bool("WORKBUDDY_STRICT_MCP_CONFIG", False),
            mcp_refresh_ms=args.mcp_refresh_ms or env_int("WORKBUDDY_MCP_REFRESH_MS", 15000),
            mcp_admin_timeout_ms=env_int("WORKBUDDY_MCP_ADMIN_TIMEOUT_MS", 12000),
            mcp_admin_cache_ms=env_int("WORKBUDDY_MCP_ADMIN_CACHE_MS", 30000),
            event_max_bytes=args.event_max_bytes or env_int("WORKBUDDY_EVENT_MAX_BYTES", 65536),
            max_turns=args.max_turns or env_int("WORKBUDDY_MAX_TURNS", 8),
            request_timeout_ms=env_int("WORKBUDDY_REQUEST_TIMEOUT_MS", 900000),
            sse_heartbeat_ms=env_int("WORKBUDDY_SSE_HEARTBEAT_MS", 15000),
            start_timeout_ms=env_int("WORKBUDDY_START_TIMEOUT_MS", 45000),
            system_prompt=os.getenv("WORKBUDDY_SYSTEM_PROMPT") or "You are serving an API chat request. Answer the user directly. Use WorkBuddy built-in tools and configured MCP tools when they help complete the request.",
            protocol_system_prompt="You are a deterministic API tool-selection adapter. Follow the client-side tool protocol in the prompt, emit the requested JSON envelope, and do not invoke internal tools.",
            configured_model_ids=configured,
        )

    def refresh_catalog(self, force: bool = False) -> bool:
        if self.configured_model_ids: return False
        current = time.time() * 1000
        if not force and current - self.last_catalog_refresh_ms < 10000: return False
        self.last_catalog_refresh_ms = current
        latest = load_model_catalog(self.cli_script, Path.home())
        available = latest["cliModels"] or [x["id"] for x in latest["models"]]
        ids = available
        if self.default_model not in ids: ids.insert(0, self.default_model)
        changed = latest["source"] != self.model_catalog_source or latest.get("updatedAt") != self.model_catalog_updated_at or ids != self.models
        if changed:
            self.models = list(dict.fromkeys(ids)); self.model_catalog = {x["id"]: x for x in latest["models"]}
            self.model_catalog_source = latest["source"]; self.model_catalog_updated_at = latest.get("updatedAt"); self.cli_models = latest["cliModels"]
            log("catalog", f"refreshed source={latest['source']} models={len(self.models)}")
        return changed

    def resolve_model(self, requested: Any) -> str:
        raw = str(requested or self.default_model).strip()
        unprefixed = raw[len("workbuddy/"):] if raw.startswith("workbuddy/") else (raw[len("anthropic/"):] if raw.startswith("anthropic/") else raw)
        model = self.aliases.get(unprefixed) or (self.default_model if re.match(r"^claude(?:[-.]|$)", unprefixed, re.I) else unprefixed)
        if model not in self.models: self.refresh_catalog(True)
        if model not in self.models:
            raise ProxyError(400, "model_not_found", f"Model '{raw}' is not configured. Available models: {', '.join(self.models)}")
        return model


class UsageLedger:
    """Small local-only usage ledger that never stores request or response content."""

    def __init__(self, filename: Path, max_records: int):
        self.filename = filename
        self.max_records = max(100, min(int(max_records), 100000))
        self.lock = threading.RLock()
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        self.records = self._load()

    def _load(self) -> list[dict[str, Any]]:
        parsed = read_json_file(self.filename)
        raw = parsed.get("records") if isinstance(parsed, dict) else parsed
        if not isinstance(raw, list):
            return []
        records: list[dict[str, Any]] = []
        for item in raw[-self.max_records:]:
            if not isinstance(item, dict):
                continue
            try:
                record = {
                    "timestamp": str(item.get("timestamp") or ""),
                    "model": slug(str(item.get("model") or "auto")),
                    "protocol": str(item.get("protocol") or "chat")[:24],
                    "input_tokens": max(0, int(item.get("input_tokens") or 0)),
                    "output_tokens": max(0, int(item.get("output_tokens") or 0)),
                    "total_tokens": max(0, int(item.get("total_tokens") or 0)),
                    "estimated": bool(item.get("estimated")),
                    "success": bool(item.get("success", True)),
                }
            except (TypeError, ValueError):
                continue
            records.append(record)
        return records

    def _save_locked(self) -> None:
        payload = {"version": 1, "records": self.records[-self.max_records:]}
        temporary = self.filename.with_suffix(self.filename.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.filename)

    def append(self, model: str, protocol: str, usage: Any, estimated: bool) -> None:
        if not isinstance(usage, dict):
            return
        try:
            input_tokens = max(0, int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0))
            output_tokens = max(0, int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0))
            total_tokens = max(0, int(usage.get("total_tokens") or input_tokens + output_tokens))
        except (TypeError, ValueError):
            return
        record = {
            "timestamp": now_iso(),
            "model": slug(model),
            "protocol": str(protocol or "chat")[:24],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated": bool(estimated),
            "success": True,
        }
        with self.lock:
            self.records.append(record)
            if len(self.records) > self.max_records:
                self.records = self.records[-self.max_records:]
            with contextlib.suppress(Exception):
                self._save_locked()

    @staticmethod
    def _is_today(timestamp: str) -> bool:
        try:
            value = _dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return value.astimezone().date() == _dt.datetime.now().astimezone().date()
        except Exception:
            return False

    def snapshot(self, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self.lock:
            values = list(self.records)
        today = [item for item in values if self._is_today(str(item.get("timestamp") or ""))]
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        ranking: dict[str, dict[str, Any]] = {}
        for item in values:
            for key in totals:
                totals[key] += int(item.get(key) or 0)
            model = str(item.get("model") or "auto")
            bucket = ranking.setdefault(model, {"model": model, "requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
            bucket["requests"] += 1
            for key in totals:
                bucket[key] += int(item.get(key) or 0)
        today_totals = {key: sum(int(item.get(key) or 0) for item in today) for key in totals}
        return {
            "updated_at": now_iso(),
            "summary": {
                "total_requests": len(values),
                "today_requests": len(today),
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "total_tokens": totals["total_tokens"],
                "today_input_tokens": today_totals["input_tokens"],
                "today_output_tokens": today_totals["output_tokens"],
                "today_total_tokens": today_totals["total_tokens"],
            },
            "ranking": sorted(ranking.values(), key=lambda item: (item["total_tokens"], item["requests"]), reverse=True)[:12],
            "records": list(reversed(values[-limit:])),
        }


class WorkBuddyAccountClient:
    """Reads the local WorkBuddy session in memory and returns a redacted account view."""

    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def _number(value: Any) -> int | float | None:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            candidate = value.strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", candidate):
                try:
                    return int(candidate) if "." not in candidate else float(candidate)
                except ValueError:
                    return None
        return None

    @staticmethod
    def _text(value: Any, maximum: int = 160) -> str:
        return str(value or "").strip()[:maximum]

    def available(self) -> bool:
        return self.config.product_config.is_file() and self.config.account_session_path.is_file()

    def _session(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        session = read_json_file(self.config.account_session_path)
        if not isinstance(session, dict):
            raise ProxyError(503, "workbuddy_account_unavailable", "未检测到 WorkBuddy 本地登录会话。")
        auth = session.get("auth") if isinstance(session.get("auth"), dict) else {}
        account = session.get("account") if isinstance(session.get("account"), dict) else {}
        token = auth.get("accessToken")
        user_id = account.get("uid") or account.get("uin") or account.get("oneidAccountId")
        if not isinstance(token, str) or not token.strip() or user_id is None or not str(user_id).strip():
            raise ProxyError(503, "workbuddy_account_session_expired", "WorkBuddy 本地会话已失效，请先在客户端完成登录。")
        product = read_json_file(self.config.product_config)
        if not isinstance(product, dict):
            raise ProxyError(503, "workbuddy_product_config_missing", "未找到 WorkBuddy 产品配置。")
        return auth, account, product

    def _post(self, route: str) -> dict[str, Any]:
        auth, account, product = self._session()
        endpoint = self._text(product.get("endpoint")) or "https://copilot.tencent.com"
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        endpoint = endpoint.rstrip("/") + route
        version = self._text(product.get("genieVersion"), 32) or "5.3.5"
        headers = {
            "Authorization": "Bearer " + str(auth["accessToken"]),
            "X-User-Id": str(account.get("uid") or account.get("uin") or account.get("oneidAccountId")),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WorkBuddy/" + version,
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Version": version,
            "X-Product": "WorkBuddy",
        }
        domain = auth.get("domain")
        if isinstance(domain, str) and domain.strip():
            headers["X-Domain"] = domain.strip()
        enterprise_id = account.get("enterpriseId") or account.get("enterprise_id")
        if enterprise_id is not None and str(enterprise_id).strip():
            headers["X-Enterprise-Id"] = str(enterprise_id)
            headers["X-Tenant-Id"] = str(enterprise_id)
        request = urllib.request.Request(endpoint, data=b"{}", method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.config.account_timeout_ms / 1000) as response:
                parsed = json.loads(response.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as exc:
            with contextlib.suppress(Exception):
                exc.close()
            if exc.code in {401, 403}:
                raise ProxyError(503, "workbuddy_account_session_expired", "WorkBuddy 本地会话已失效，请先在客户端重新登录。") from exc
            raise ProxyError(502, "workbuddy_account_upstream_error", f"WorkBuddy 账户服务返回 HTTP {exc.code}。") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise ProxyError(502, "workbuddy_account_connect_error", "WorkBuddy 账户服务暂时不可达，请检查网络或系统代理。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProxyError(502, "workbuddy_account_invalid_response", "WorkBuddy 账户服务返回了无法识别的数据。") from exc
        if not isinstance(parsed, dict):
            raise ProxyError(502, "workbuddy_account_invalid_response", "WorkBuddy 账户服务返回了无法识别的数据。")
        code = parsed.get("code")
        if code not in {None, 0, "0"}:
            raise ProxyError(502, "workbuddy_account_request_failed", "WorkBuddy 账户服务未完成本次请求。")
        return parsed

    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        value = payload.get("data")
        return value if isinstance(value, dict) else {}

    def checkin(self) -> dict[str, Any]:
        data = self._data(self._post("/v2/billing/meter/checkin-activity-status"))
        progress = data.get("week_progress")
        return {
            "available": True,
            "active": bool(data.get("active")),
            "today_checked_in": bool(data.get("today_checked_in")),
            "streak_days": int(self._number(data.get("streak_days")) or 0),
            "daily_credit": self._number(data.get("daily_credit")) or 0,
            "today_credit": self._number(data.get("today_credit")) or 0,
            "total_credits": self._number(data.get("total_credits")) or 0,
            "is_streak_day": bool(data.get("is_streak_day")),
            "next_streak_day": int(self._number(data.get("next_streak_day")) or 0),
            "streak_bonus_days": data.get("streak_bonus_days") if isinstance(data.get("streak_bonus_days"), list) else [],
            "streak_bonus_credit": self._number(data.get("streak_bonus_credit")) or 0,
            "checkin_dates": [self._text(item, 24) for item in data.get("checkin_dates", []) if isinstance(item, str)][:31],
            "week_progress": [bool(item) for item in progress][:7] if isinstance(progress, list) else [],
            "activity_name": self._text(data.get("activity_name")),
            "theme_name": self._text(data.get("theme_name")),
            "season": self._text(data.get("season"), 40),
            "start_time": self._text(data.get("start_time"), 40),
            "end_time": self._text(data.get("end_time"), 40),
            "claim_button_text": self._text(data.get("claim_button_text"), 80),
        }

    def claim_checkin(self) -> dict[str, Any]:
        self._post("/v2/billing/meter/daily-checkin")
        status = self.checkin()
        return {"status": "ok", "claimed": bool(status.get("today_checked_in")), "checkin": status}

    def account(self) -> dict[str, Any]:
        payload = self._data(self._post("/v2/billing/meter/get-user-resource"))
        response = payload.get("Response") if isinstance(payload.get("Response"), dict) else {}
        data = response.get("Data") if isinstance(response.get("Data"), dict) else {}
        raw_resources = data.get("Accounts") if isinstance(data.get("Accounts"), list) else []
        resources: list[dict[str, Any]] = []
        for item in raw_resources[:100]:
            if not isinstance(item, dict):
                continue
            total = self._number(item.get("CapacitySizePrecise"))
            remaining = self._number(item.get("CapacityRemainPrecise"))
            used = self._number(item.get("CapacityUsedPrecise"))
            total = self._number(item.get("CapacitySize")) if total is None else total
            remaining = self._number(item.get("CapacityRemain")) if remaining is None else remaining
            used = self._number(item.get("CapacityUsed")) if used is None else used
            resources.append({
                "name": self._text(item.get("PackageName") or item.get("ProductName") or item.get("DealName"), 120),
                "product": self._text(item.get("ProductName") or item.get("SubProductName"), 120),
                "unit": self._text(item.get("CapacityUnit") or item.get("OriginUnit"), 32),
                "total": total,
                "remaining": remaining,
                "used": used,
                "cycle_total": self._number(item.get("CycleCapacitySizePrecise")) if self._number(item.get("CycleCapacitySizePrecise")) is not None else self._number(item.get("CycleCapacitySize")),
                "cycle_remaining": self._number(item.get("CycleCapacityRemainPrecise")) if self._number(item.get("CycleCapacityRemainPrecise")) is not None else self._number(item.get("CycleCapacityRemain")),
                "cycle_used": self._number(item.get("CycleCapacityUsedPrecise")) if self._number(item.get("CycleCapacityUsedPrecise")) is not None else self._number(item.get("CycleCapacityUsed")),
                "cycle_start": self._text(item.get("CycleStartTime"), 40),
                "cycle_end": self._text(item.get("CycleEndTime") or item.get("ExpiredTime"), 40),
                "status": int(self._number(item.get("Status")) or 0),
            })
        balances: dict[str, dict[str, Any]] = {}
        for resource in resources:
            unit = resource["unit"] or "额度"
            bucket = balances.setdefault(unit, {"unit": unit, "remaining": 0, "used": 0, "total": 0, "resources": 0})
            bucket["resources"] += 1
            for key in ("remaining", "used", "total"):
                value = resource.get(key)
                if isinstance(value, (int, float)):
                    bucket[key] += value
        primary = max(resources, key=lambda item: float(item.get("remaining") or 0), default=None)
        return {
            "available": True,
            "updated_at": now_iso(),
            "resource_count": len(resources),
            "primary_balance": primary,
            "balances": list(balances.values()),
            "resources": resources,
        }


def parse_mcp_config(value: str) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw: return {"mcpServers": {}}
    if raw.startswith("{"): parsed = json.loads(raw)
    else: parsed = json.loads(Path(raw).expanduser().resolve().read_text(encoding="utf-8-sig"))
    return parsed if isinstance(parsed, dict) else {"mcpServers": {}}


def mcp_fingerprint(value: str) -> str:
    try: raw = stable_json(parse_mcp_config(value))
    except Exception: raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mcp_transport(server: dict[str, Any]) -> str:
    typ = str(server.get("type", "")).lower()
    if typ == "stdio" or server.get("command"): return "stdio"
    if typ == "sse": return "sse"
    if server.get("url"): return typ or "streamable-http"
    return typ or "unknown"


def redact_url(raw: Any) -> Optional[str]:
    if not raw: return None
    try:
        parsed = urllib.parse.urlsplit(str(raw)); query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(k, "[redacted]" if re.search(r"token|key|secret|auth|signature|credential", k, re.I) else v) for k, v in query]
        host = parsed.hostname or ""; netloc = host
        if ":" in host and not host.startswith("["): netloc = f"[{host}]"
        if parsed.port: netloc += f":{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment))
    except Exception:
        return str(raw)


def redact_args(args: Any) -> list[str]:
    values = [str(x) for x in args] if isinstance(args, list) else []; result = []; hide = False
    for value in values:
        if hide: result.append("[redacted]"); hide = False; continue
        if re.match(r"^--?(?:token|api[-_]?key|secret|auth|password|credential)$", value, re.I): hide = True; result.append(value)
        else: result.append(re.sub(r"(--?(?:token|api[-_]?key|secret|auth|password|credential)=).+", r"\1[redacted]", value, flags=re.I))
    return result


def parse_jsonrpc_text(text: str, content_type: str = "") -> Any:
    raw = str(text or "").strip()
    if not raw: return None
    if "text/event-stream" in content_type.lower() or re.search(r"^(?:event|data):", raw, re.M):
        messages = []
        for block in re.split(r"\r?\n\r?\n", raw):
            data = "\n".join(line[5:].lstrip() for line in re.split(r"\r?\n", block) if line.startswith("data:"))
            if data and data != "[DONE]":
                try: messages.append(json.loads(data))
                except Exception: pass
        return messages[-1] if messages else None
    try: return json.loads(raw)
    except Exception: pass
    for line in reversed(raw.splitlines()):
        try: return json.loads(line)
        except Exception: pass
    return None


def urlopen_request(url: str, method: str = "GET", headers: Optional[dict[str, str]] = None, body: Any = None, timeout: float = 30):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else (body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise ProxyError(502 if exc.code >= 500 else exc.code, "upstream_http_error", raw or f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ProxyError(502, "upstream_connect_error", str(getattr(exc, "reason", exc))) from exc



class StdioMcpSession:
    def __init__(self, server: dict[str, Any], timeout_ms: int):
        self.server, self.timeout = server, timeout_ms / 1000
        self.process: Optional[subprocess.Popen] = None; self.messages: queue.Queue = queue.Queue(); self.next_id = 1
        self.protocol_version = MCP_PROTOCOL_VERSION; self.server_info = None; self.stderr = ""

    def start(self) -> None:
        command = self.server.get("command")
        if not command: raise RuntimeError("STDIO MCP server is missing command.")
        env = os.environ.copy(); env.update({str(k): str(v) for k, v in self.server.get("env", {}).items()})
        self.process = subprocess.Popen([str(command), *[str(x) for x in self.server.get("args", [])]], cwd=self.server.get("cwd") or None, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._reader, daemon=True).start(); threading.Thread(target=self._stderr_reader, daemon=True).start()
        initialized = self.request("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": f"{APP_NAME}-admin", "version": VERSION}})
        self.protocol_version = initialized.get("protocolVersion", MCP_PROTOCOL_VERSION) if isinstance(initialized, dict) else MCP_PROTOCOL_VERSION
        self.server_info = initialized.get("serverInfo") if isinstance(initialized, dict) else None
        self.notify("notifications/initialized", {})

    def _reader(self) -> None:
        assert self.process and self.process.stdout
        buffer = b""
        while True:
            chunk = self.process.stdout.read(1)
            if not chunk: break
            buffer += chunk
            header = re.match(br"^Content-Length:\s*(\d+)\r?\n\r?\n", buffer, re.I)
            if header:
                length = int(header.group(1)); start = header.end()
                if len(buffer) >= start + length:
                    raw, buffer = buffer[start:start + length], buffer[start + length:]
                    self._put_message(raw)
                continue
            if b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip(): self._put_message(line.strip())

    def _put_message(self, raw: bytes) -> None:
        try: self.messages.put(json.loads(raw.decode("utf-8", "replace")))
        except Exception: pass

    def _stderr_reader(self) -> None:
        assert self.process and self.process.stderr
        while True:
            chunk = self.process.stderr.read(1024)
            if not chunk: break
            self.stderr = (self.stderr + chunk.decode("utf-8", "replace"))[-8192:]

    def request(self, method: str, params: dict[str, Any]) -> Any:
        if not self.process or not self.process.stdin: raise RuntimeError("MCP STDIO session is not writable.")
        request_id = self.next_id; self.next_id += 1
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")); self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try: message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty: break
            if message.get("id") != request_id: continue
            if message.get("error"): raise RuntimeError(message["error"].get("message") or stable_json(message["error"]))
            return message.get("result")
        raise TimeoutError(f"{method} timed out after {int(self.timeout * 1000)}ms")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.process and self.process.stdin:
            self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, separators=(",", ":")) + "\n").encode()); self.process.stdin.flush()

    def close(self) -> None:
        if not self.process: return
        with contextlib.suppress(Exception):
            if self.process.stdin: self.process.stdin.close()
        with contextlib.suppress(Exception): self.process.terminate(); self.process.wait(timeout=1)
        if self.process.poll() is None:
            with contextlib.suppress(Exception): self.process.kill()


class HttpMcpSession:
    def __init__(self, server: dict[str, Any], timeout_ms: int):
        self.server, self.timeout = server, timeout_ms / 1000; self.next_id = 1; self.session_id = ""
        self.protocol_version = MCP_PROTOCOL_VERSION; self.server_info = None

    def headers(self) -> dict[str, str]:
        result = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **{str(k): str(v) for k, v in self.server.get("headers", {}).items()}}
        if self.session_id: result.update({"Mcp-Session-Id": self.session_id, "MCP-Protocol-Version": self.protocol_version})
        return result

    def post(self, payload: dict[str, Any], expect: bool = True) -> Any:
        response = urlopen_request(str(self.server.get("url")), "POST", self.headers(), payload, self.timeout)
        session = response.headers.get("mcp-session-id")
        if session: self.session_id = session
        if not expect or response.status in {202, 204}: response.close(); return None
        text = response.read().decode("utf-8", "replace"); content_type = response.headers.get("content-type", ""); response.close()
        message = parse_jsonrpc_text(text, content_type)
        if not isinstance(message, dict): raise RuntimeError("MCP HTTP response did not contain JSON-RPC data.")
        if message.get("error"): raise RuntimeError(message["error"].get("message") or stable_json(message["error"]))
        return message.get("result")

    def start(self) -> None:
        initialized = self.request("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": f"{APP_NAME}-admin", "version": VERSION}})
        if isinstance(initialized, dict):
            self.protocol_version = initialized.get("protocolVersion", MCP_PROTOCOL_VERSION); self.server_info = initialized.get("serverInfo")
        self.notify("notifications/initialized", {})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self.next_id; self.next_id += 1
        return self.post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, True)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.post({"jsonrpc": "2.0", "method": method, "params": params}, False)

    def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(Exception): urlopen_request(str(self.server.get("url")), "DELETE", self.headers(), None, min(self.timeout, 2)).close()


class McpAdminManager:
    def __init__(self, config_value: str, source: str, server_sources: dict[str, str], timeout_ms: int, cache_ms: int):
        self.timeout_ms, self.cache_ms = timeout_ms, cache_ms; self.cache: dict[str, dict[str, Any]] = {}; self.lock = threading.Lock(); self.fingerprint = ""
        self.update(config_value, source, server_sources)

    def update(self, config_value: str, source: str, server_sources: dict[str, str]) -> bool:
        fingerprint = mcp_fingerprint(config_value); changed = bool(self.fingerprint and self.fingerprint != fingerprint)
        self.config_value, self.source, self.server_sources, self.fingerprint = config_value, source, server_sources or {}, fingerprint
        self.parse_error = None
        try:
            parsed = parse_mcp_config(config_value); self.servers = parsed.get("mcpServers", {}) if isinstance(parsed.get("mcpServers"), dict) else {}
        except Exception as exc:
            self.servers = {}; self.parse_error = str(exc)
        if changed: self.cache.clear()
        return changed

    def summary(self) -> dict[str, Any]:
        names = list(self.servers)
        return {"configured": bool(names), "source": self.source, "server_count": len(names), "enabled_server_count": sum(1 for x in names if self.servers[x].get("disabled") is not True), "fingerprint": self.fingerprint[:12], "parse_error": self.parse_error}

    def list_servers(self) -> list[dict[str, Any]]:
        result = []
        for name, server in self.servers.items():
            cached = self.cache.get(name, {})
            result.append({"name": name, "source": self.server_sources.get(name, self.source), "enabled": server.get("disabled") is not True, "transport": mcp_transport(server), "description": server.get("description") if isinstance(server.get("description"), str) else None, "command": str(server.get("command")) if server.get("command") else None, "args": redact_args(server.get("args")), "cwd": str(server.get("cwd")) if server.get("cwd") else None, "url": redact_url(server.get("url")), "header_names": list(server.get("headers", {})) if isinstance(server.get("headers"), dict) else [], "env_names": list(server.get("env", {})) if isinstance(server.get("env"), dict) else [], "status": cached.get("status", "not_tested"), "last_checked_at": cached.get("checked_at"), "latency_ms": cached.get("latency_ms"), "tool_count": len(cached.get("tools", [])) if "tools" in cached else None, "error": cached.get("error")})
        return result

    def _session(self, server: dict[str, Any]):
        transport = mcp_transport(server)
        if transport == "stdio": return StdioMcpSession(server, self.timeout_ms)
        if server.get("url") and transport != "sse": return HttpMcpSession(server, self.timeout_ms)
        raise RuntimeError(f"Unsupported MCP transport: {transport}")

    def inspect(self, name: str, force: bool = False, call_spec: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        server = self.servers.get(name)
        if not server: raise RuntimeError(f"MCP server not found: {name}")
        if server.get("disabled") is True: raise RuntimeError(f"MCP server is disabled: {name}")
        cached = self.cache.get(name)
        if not force and not call_spec and cached and cached.get("status") == "ok" and time.time() * 1000 - cached["checked_ms"] < self.cache_ms: return cached
        started = time.time(); session = self._session(server)
        try:
            session.start(); raw_tools = []; cursor = None
            while True:
                listed = session.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
                raw_tools.extend(listed.get("tools", []) if isinstance(listed.get("tools"), list) else [])
                cursor = listed.get("nextCursor")
                if not cursor: break
            tools = [{"server": name, "name": str(x.get("name", "")), "workbuddy_name": f"mcp__{name}__{x.get('name', '')}", "title": x.get("title"), "description": x.get("description"), "input_schema": x.get("inputSchema") or {"type": "object"}, "output_schema": x.get("outputSchema"), "annotations": x.get("annotations")} for x in raw_tools]
            call = None
            if call_spec and call_spec.get("tool"):
                if not any(x.get("name") == call_spec["tool"] for x in raw_tools): raise RuntimeError(f"MCP tool not found: {call_spec['tool']}")
                call = session.request("tools/call", {"name": call_spec["tool"], "arguments": call_spec.get("arguments") if isinstance(call_spec.get("arguments"), dict) else {}})
            record = {"status": "ok", "checked_at": now_iso(), "checked_ms": time.time() * 1000, "latency_ms": round((time.time() - started) * 1000), "protocol_version": session.protocol_version, "server_info": session.server_info, "tools": tools, "call": call, "error": None}
        except Exception as exc:
            record = {"status": "error", "checked_at": now_iso(), "checked_ms": time.time() * 1000, "latency_ms": None, "protocol_version": None, "server_info": None, "tools": [], "call": None, "error": str(exc)}
        finally:
            with contextlib.suppress(Exception): session.close()
        self.cache[name] = record
        return record

    def list_tools(self, server: str = "", force: bool = False) -> dict[str, Any]:
        names = [server] if server else [x for x, value in self.servers.items() if value.get("disabled") is not True]
        results = [(name, self.inspect(name, force)) for name in names]; tools = [tool for _, result in results for tool in result.get("tools", [])]
        return {"object": "list", "data": tools, "tool_count": len(tools), "servers": [{"name": name, "status": result["status"], "latency_ms": result["latency_ms"], "protocol_version": result["protocol_version"], "server_info": result["server_info"], "tool_count": len(result.get("tools", [])), "error": result["error"], "checked_at": result["checked_at"]} for name, result in results]}

    def test(self, body: dict[str, Any]) -> dict[str, Any]:
        server = str(body.get("server", ""))
        if not server: raise RuntimeError("MCP test requires server.")
        tool = str(body.get("tool", "")); result = self.inspect(server, True, {"tool": tool, "arguments": body.get("arguments", {})} if tool else None)
        return {"server": server, "status": result["status"], "latency_ms": result["latency_ms"], "protocol_version": result["protocol_version"], "server_info": result["server_info"], "tools": result["tools"], "tool_count": len(result["tools"]), "call": result["call"], "error": result["error"], "checked_at": result["checked_at"]}



@dataclass
class GatewayRecord:
    key: str
    model: str
    profile: str
    port: int = 0
    base_url: str = ""
    process: Optional[subprocess.Popen] = None
    ready: bool = False


class GatewayManager:
    def __init__(self, config: Config):
        self.config = config; self.instances: dict[str, GatewayRecord] = {}; self.lock = threading.RLock()

    def get(self, model: str, profile: str = "agent") -> GatewayRecord:
        key = f"{model}::{profile}"
        with self.lock:
            current = self.instances.get(key)
            if current and current.ready and current.process and current.process.poll() is None: return current
            if current: self.instances.pop(key, None)
            record = GatewayRecord(key, model, profile); self.instances[key] = record
            try: return self._start(record)
            except Exception:
                self.instances.pop(key, None); raise

    def _start(self, record: GatewayRecord) -> GatewayRecord:
        port = get_free_port(); model_slug = slug(f"{record.model}-{record.profile}")
        out_path = RUNTIME_DIR / f"gateway-{model_slug}.out.log"; err_path = RUNTIME_DIR / f"gateway-{model_slug}.err.log"
        out = open(out_path, "ab", buffering=0); err = open(err_path, "ab", buffering=0)
        out.write(f"\n=== {now_iso()} starting model={record.model} profile={record.profile} ===\n".encode()); err.write(f"\n=== {now_iso()} starting model={record.model} profile={record.profile} ===\n".encode())
        protocol = record.profile == "protocol"
        args = [str(self.config.workbuddy_exe), str(self.config.cli_script), "--serve", "--port", str(port), "--host", "127.0.0.1", "--session-id", f"wb-api-{model_slug}-{uuid.uuid4().hex[:8]}", "--model", record.model, "--max-turns", str(1 if protocol else self.config.max_turns), "--system-prompt", self.config.protocol_system_prompt if protocol else self.config.system_prompt]
        if protocol or self.config.disable_tools: args.append("--tools=")
        else:
            args += ["--permission-mode", self.config.permission_mode, "--tools", self.config.tools or "default"]
            if self.config.allowed_tools: args += ["--allowedTools", *self.config.allowed_tools]
            if self.config.disallowed_tools: args += ["--disallowedTools", *self.config.disallowed_tools]
            if self.config.add_dirs: args += ["--add-dir", *self.config.add_dirs]
            if self.config.mcp_config: args += ["--mcp-config", self.config.mcp_config]
            if self.config.strict_mcp_config: args.append("--strict-mcp-config")
        env = os.environ.copy(); env.update({"ELECTRON_RUN_AS_NODE": "1", "NO_COLOR": "1", "CODEBUDDY_GATEWAY_AUTH": "none"})
        log("gateway", f"starting model={record.model} profile={record.profile} port={port}")
        try:
            process = subprocess.Popen(args, cwd=str(self.config.cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            out.close(); err.close(); raise
        record.process, record.port, record.base_url = process, port, f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.config.start_timeout_ms / 1000; last_error = ""
        while time.monotonic() < deadline:
            if process.poll() is not None: raise ProxyError(502, "gateway_start_failed", f"WorkBuddy gateway exited with code {process.returncode}.", {"stderr": str(err_path)})
            try:
                response = urlopen_request(record.base_url + "/api/v1/health", "GET", {"X-CodeBuddy-Request": "1"}, timeout=2)
                response.close(); record.ready = True; log("gateway", f"ready model={record.model} profile={record.profile} port={port}"); return record
            except Exception as exc: last_error = str(exc)
            time.sleep(.4)
        with contextlib.suppress(Exception): process.kill()
        raise ProxyError(504, "gateway_start_timeout", f"WorkBuddy gateway startup timed out for model {record.model}.", {"lastError": last_error, "stderr": str(err_path)})

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [{"model": x.model, "profile": x.profile, "tools_enabled": x.profile == "agent" and not self.config.disable_tools, "port": x.port, "ready": x.ready and bool(x.process and x.process.poll() is None), "pid": x.process.pid if x.process else None} for x in self.instances.values()]

    def stop_profile(self, profile: str) -> int:
        return self.stop_where(lambda x: x.profile == profile)

    def stop_where(self, predicate: Callable[[GatewayRecord], bool]) -> int:
        with self.lock:
            records = [x for x in self.instances.values() if predicate(x)]
            for x in records: self.instances.pop(x.key, None)
        for record in records:
            process = record.process
            if not process or process.poll() is not None: continue
            with contextlib.suppress(Exception): process.terminate(); process.wait(timeout=3)
            if process.poll() is None:
                with contextlib.suppress(Exception): process.kill()
        return len(records)

    def stop_all(self) -> int:
        return self.stop_where(lambda _: True)


def iter_sse_response(response) -> Iterable[tuple[str, str]]:
    event, data = "message", []
    while True:
        raw = response.readline()
        if not raw:
            if data: yield event, "\n".join(data)
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data: yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith("event:"): event = line[6:].strip()
        elif line.startswith("data:"): data.append(line[5:].lstrip())


def extract_event_text(message: Any) -> str:
    if not isinstance(message, dict): return ""
    content = message.get("content")
    if isinstance(content, str): return content
    if isinstance(content, dict):
        if isinstance(content.get("markdown"), str): return content["markdown"]
        if isinstance(content.get("text"), str): return content["text"]
    for parent in (message.get("payload"), message.get("result")):
        if isinstance(parent, dict) and isinstance(parent.get("text"), str): return parent["text"]
    return ""


def text_from_content(value: Any) -> str:
    if isinstance(value, str): return value
    if value is None: return ""
    if isinstance(value, list): return "\n".join(filter(None, (text_from_content(x) for x in value)))
    if isinstance(value, dict):
        if isinstance(value.get("text"), str): return value["text"]
        if value.get("content") is not None: return text_from_content(value["content"])
    return ""


def bounded_value(value: Any, max_bytes: int) -> Any:
    if value is None: return None
    try: encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception: encoded = json.dumps(str(value), ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= max_bytes: return value
    marker = f"…[truncated at {max_bytes} bytes]"; budget = max(0, max_bytes - len(json.dumps(marker).encode()) - 4)
    return encoded.encode("utf-8")[:budget].decode("utf-8", "ignore") + marker


def usage_from_update(update: dict[str, Any]) -> Optional[dict[str, Any]]:
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}; raw = meta.get("usage")
    if not isinstance(raw, dict): return None
    result = dict(raw); prompt = int(raw.get("prompt_tokens") or 0); completion = int(raw.get("completion_tokens") or 0)
    result.update({"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": int(raw.get("total_tokens") or prompt + completion)})
    return result


def normalize_acp_event(update: dict[str, Any], max_bytes: int) -> Optional[dict[str, Any]]:
    typ = update.get("sessionUpdate"); meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
    if typ == "tool_call": return {"type": "workbuddy.tool_call", "id": update.get("toolCallId"), "name": meta.get("codebuddy.ai/toolName") or update.get("name") or update.get("title"), "title": update.get("title"), "kind": update.get("kind"), "status": update.get("status"), "input": bounded_value(update.get("rawInput", update.get("input", {})), max_bytes), "locations": bounded_value(update.get("locations", []), max_bytes)}
    if typ == "tool_call_update" and update.get("status") in {"completed", "failed", "cancelled"}:
        result = update.get("rawOutput", update.get("output", update.get("content")))
        return {"type": "workbuddy.tool_result", "id": update.get("toolCallId"), "name": meta.get("codebuddy.ai/toolName") or update.get("name"), "status": update.get("status"), "result": bounded_value(result, max_bytes), "text": bounded_value(text_from_content(result), max_bytes)}
    if typ == "usage_update":
        usage = usage_from_update(update)
        return {"type": "workbuddy.usage", "usage": bounded_value(usage, max_bytes), "cost": bounded_value(update.get("cost"), max_bytes)} if usage else None
    if typ == "session_info_update":
        phase = meta.get("codebuddy.ai/agentPhase")
        if isinstance(phase, dict) and phase.get("phase"): return {"type": "workbuddy.phase", "phase": phase["phase"], "started_at": phase.get("startedAt")}
    if typ == "plan": return {"type": "workbuddy.plan", "plan": bounded_value(update.get("plan", update.get("entries", update)), max_bytes)}
    if typ == "interruption_request": return {"type": "workbuddy.interruption", "id": update.get("toolCallId"), "name": meta.get("codebuddy.ai/toolName") or update.get("name"), "title": update.get("title")}
    if typ == "session_end": return {"type": "workbuddy.session_end", "stop_reason": update.get("stopReason")}
    return None



def aggregate_usage(values: Iterable[dict[str, Any]]) -> Optional[dict[str, int]]:
    items = list(values)
    if not items: return None
    prompt = sum(int(x.get("prompt_tokens") or 0) for x in items)
    completion = sum(int(x.get("completion_tokens") or 0) for x in items)
    total = sum(int(x.get("total_tokens") or 0) for x in items)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total or prompt + completion}


def run_acp_session(base_url: str, cwd: Path, prompt: str, timeout_ms: int, max_event_bytes: int,
                    on_delta: Optional[Callable[[str, Any], None]] = None,
                    on_event: Optional[Callable[[dict[str, Any]], None]] = None) -> dict[str, Any]:
    timeout = timeout_ms / 1000
    connection_id: Optional[str] = None; session_id: Optional[str] = None; rpc_id = 0
    events: list[dict[str, Any]] = []; usage_map: dict[str, dict[str, Any]] = {}; emitted_usage: set[str] = set()
    output: list[str] = []; last_phase: Optional[str] = None

    def emit(event: Optional[dict[str, Any]]) -> None:
        nonlocal last_phase
        if not event: return
        if event.get("type") == "workbuddy.phase":
            if event.get("phase") == last_phase: return
            last_phase = str(event.get("phase"))
        events.append(event)
        if on_event: on_event(event)

    def handle_message(message: Any) -> None:
        if not isinstance(message, dict) or message.get("method") != "session/update": return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        update = params.get("update")
        if not isinstance(update, dict): return
        if update.get("sessionUpdate") == "agent_message_chunk":
            delta = text_from_content(update.get("content"))
            if delta:
                output.append(delta)
                if on_delta: on_delta(delta, update)
            return
        if update.get("sessionUpdate") == "usage_update":
            usage = usage_from_update(update)
            if usage:
                meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
                key = str(meta.get("codebuddy.ai/messageId") or meta.get("codebuddy.ai/requestId") or len(usage_map))
                usage_map[key] = usage
                marker = f"{key}:{usage['prompt_tokens']}:{usage['completion_tokens']}:{usage['total_tokens']}"
                if marker in emitted_usage: return
                emitted_usage.add(marker)
        emit(normalize_acp_event(update, max_event_bytes))

    def rpc(method: str, params: Optional[dict[str, Any]] = None, notification: bool = False) -> Any:
        nonlocal rpc_id
        if not notification: rpc_id += 1
        call_id = None if notification else rpc_id
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if call_id is not None: payload["id"] = call_id
        response = urlopen_request(
            base_url + "/api/v1/acp", "POST",
            {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json, text/event-stream",
             "X-CodeBuddy-Request": "1", "acp-connection-id": str(connection_id)}, payload, timeout)
        content_type = response.headers.get("Content-Type", "")
        reply = None
        if "application/json" in content_type.lower():
            value = json.loads(response.read().decode("utf-8", "replace") or "null")
            if isinstance(value, dict) and value.get("id") == call_id: reply = value
            else: handle_message(value)
        else:
            for _, raw in iter_sse_response(response):
                if raw == "[DONE]": continue
                try: value = json.loads(raw)
                except Exception: continue
                if isinstance(value, dict) and value.get("id") == call_id: reply = value
                else: handle_message(value)
        response.close()
        if notification: return None
        if not reply: raise ProxyError(502, "acp_protocol_error", f"ACP {method} response did not include JSON-RPC result.")
        if reply.get("error"):
            error = reply["error"] if isinstance(reply["error"], dict) else {}
            raise ProxyError(502, str(error.get("code") or "acp_rpc_error"), str(error.get("message") or f"ACP {method} failed."))
        return reply.get("result")

    try:
        response = urlopen_request(base_url + "/api/v1/acp/connect", "POST",
                                   {"Content-Type": "application/json; charset=utf-8", "X-CodeBuddy-Request": "1"}, {}, timeout)
        credentials = json.loads(response.read().decode("utf-8", "replace") or "{}")
        response.close(); connection_id = credentials.get("connectionId") if isinstance(credentials, dict) else None
        if not connection_id: raise ProxyError(502, "acp_protocol_error", "ACP connection response did not include connectionId.")
        rpc("initialize", {"protocolVersion": 1, "clientInfo": {"name": APP_NAME, "version": VERSION}, "clientCapabilities": {}})
        created = rpc("session/new", {"cwd": str(cwd), "mcpServers": []})
        session_id = created.get("sessionId") if isinstance(created, dict) else None
        if not session_id: raise ProxyError(502, "acp_protocol_error", "ACP session/new response did not include sessionId.")
        result = rpc("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": str(prompt or "")} ]})
        return {"text": "".join(output), "events": events, "usage": aggregate_usage(usage_map.values()),
                "stopReason": result.get("stopReason", "end_turn") if isinstance(result, dict) else "end_turn"}
    finally:
        if connection_id:
            with contextlib.suppress(Exception):
                response = urlopen_request(base_url + "/api/v1/acp", "DELETE",
                                           {"X-CodeBuddy-Request": "1", "acp-connection-id": connection_id}, timeout=2)
                response.close()


def create_upstream_run(gateway: GatewayRecord, prompt: str, conversation: str, timeout_ms: int) -> str:
    request_id = uid()
    body = {
        "version": "1.0", "id": request_id, "type": "message",
        "source": {"platform": "generic", "sender": {"id": conversation, "name": "OpenAI-compatible client"},
                   "conversation": {"id": conversation, "type": "direct"}},
        "payload": {"text": prompt},
    }
    response = urlopen_request(
        gateway.base_url + "/api/v1/runs", "POST",
        {"Content-Type": "application/json; charset=utf-8", "X-CodeBuddy-Request": "1", "X-CodeBuddy-Run-Timeout": str(timeout_ms)},
        body, timeout_ms / 1000)
    value = json.loads(response.read().decode("utf-8", "replace") or "{}")
    response.close()
    data = value.get("data") if isinstance(value, dict) and isinstance(value.get("data"), dict) else value
    run_id = data.get("runId") if isinstance(data, dict) else None
    if not run_id: raise ProxyError(502, "upstream_protocol_error", "WorkBuddy run response did not include runId.")
    return str(run_id)


def consume_upstream_run(gateway: GatewayRecord, run_id: str, timeout_ms: int,
                         on_delta: Optional[Callable[[str, Any], None]] = None) -> str:
    url = gateway.base_url + "/api/v1/runs/" + urllib.parse.quote(run_id, safe="") + "/stream"
    response = urlopen_request(url, "GET", {"Accept": "text/event-stream", "X-CodeBuddy-Request": "1"}, timeout=timeout_ms / 1000)
    output = ""
    try:
        for event, raw in iter_sse_response(response):
            if event != "message" or raw == "[DONE]": continue
            try: message = json.loads(raw)
            except Exception: continue
            text = extract_event_text(message)
            if not text: continue
            if text.startswith(output): delta = text[len(output):]
            elif output.endswith(text): delta = ""
            else: delta = text
            if delta:
                output += delta
                if on_delta: on_delta(delta, message)
    finally:
        response.close()
    return output


def consume_generation(app: "ProxyApplication", model: str, prompt: str, conversation: str, profile: str = "agent",
                       workbuddy_events: bool = False, on_delta: Optional[Callable[[str, Any], None]] = None,
                       on_event: Optional[Callable[[dict[str, Any]], None]] = None) -> dict[str, Any]:
    if workbuddy_events and profile == "agent":
        app.refresh_mcp(False, True)
        gateway = app.gateways.get(model, "agent")
        return run_acp_session(gateway.base_url, app.config.cwd, prompt, app.config.request_timeout_ms,
                               app.config.event_max_bytes, on_delta, on_event)
    gateway = app.gateways.get(model, profile)
    run_id = create_upstream_run(gateway, prompt, conversation, app.config.request_timeout_ms)
    text = consume_upstream_run(gateway, run_id, app.config.request_timeout_ms, on_delta)
    return {"text": text, "events": [], "usage": None, "stopReason": "end_turn"}


def resolve_client_tool_output(app: "ProxyApplication", model: str, plan: dict[str, Any], conversation: str,
                               generation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed = parse_client_tool_output(generation.get("text", ""), plan["tools"], plan["choice"])
    if parsed["type"] != "invalid":
        return generation, parsed
    log("protocol", f"repairing invalid client tool output: {parsed['reason']}")
    repaired = consume_generation(app, model, build_protocol_repair_prompt(plan["tools"], plan["choice"]),
                                  conversation, "protocol")
    parsed = parse_client_tool_output(repaired.get("text", ""), plan["tools"], plan["choice"])
    if parsed["type"] == "invalid":
        raise ProxyError(
            502,
            "tool_protocol_error",
            f"WorkBuddy did not return a valid client tool protocol response after one repair attempt: {parsed['reason']}.",
        )
    return repaired, parsed


def normalize_content(content: Any) -> str:
    if isinstance(content, str): return content
    if content is None: return ""
    if not isinstance(content, list): return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    parts: list[str] = []
    for part in content:
        if isinstance(part, str): parts.append(part); continue
        if not isinstance(part, dict): continue
        typ = part.get("type")
        if typ in {"text", "input_text", "output_text"}: value = part.get("text", "")
        elif typ == "image_url":
            image = part.get("image_url"); url = image if isinstance(image, str) else (image.get("url") if isinstance(image, dict) else None)
            value = f"[Image URL: {url}]" if url else "[Image]"
        elif typ == "input_image": value = f"[Image URL: {part['image_url']}]" if part.get("image_url") else "[Image]"
        elif typ == "image":
            source = part.get("source") if isinstance(part.get("source"), dict) else {}
            value = f"[Image URL: {source['url']}]" if source.get("type") == "url" and source.get("url") else (f"[Image: {source['media_type']}]" if source.get("media_type") else "[Image]")
        elif typ == "document":
            source = part.get("source") if isinstance(part.get("source"), dict) else {}
            value = f"[Document: {part.get('title') or source.get('url') or source.get('media_type') or 'document'}]"
        elif typ == "tool_result": value = "[Tool result]\n" + normalize_content(part.get("content"))
        elif typ == "tool_use": value = f"[Tool use: {part.get('name') or 'tool'}]\n" + json.dumps(part.get("input") or {}, ensure_ascii=False, separators=(",", ":"))
        elif typ == "thinking": value = str(part.get("thinking") or "")
        else: value = str(part.get("text")) if isinstance(part.get("text"), str) else json.dumps(part, ensure_ascii=False, separators=(",", ":"))
        if value: parts.append(value)
    return "\n".join(parts)


def apply_response_format(prompt: str, fmt: Any) -> str:
    if not isinstance(fmt, dict): return prompt
    if fmt.get("type") == "json_object": return prompt + "\n\nReturn one valid JSON object with no markdown fence."
    if fmt.get("type") == "json_schema":
        block = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else {}
        schema = block.get("schema") if isinstance(block.get("schema"), dict) else (fmt.get("schema") or {})
        return prompt + "\n\nReturn JSON matching this schema, with no markdown fence:\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return prompt


def messages_to_prompt(messages: Any, request: Optional[dict[str, Any]] = None) -> str:
    if not isinstance(messages, list) or not messages: raise ProxyError(400, "invalid_request_error", "messages must be a non-empty array.")
    request = request or {}
    if len(messages) == 1 and isinstance(messages[0], dict) and messages[0].get("role") == "user" and isinstance(messages[0].get("content"), str):
        return apply_response_format(messages[0]["content"], request.get("response_format"))
    labels = {"system": "System", "developer": "Developer", "user": "User", "assistant": "Assistant", "tool": "Tool result", "function": "Function result"}
    rendered = []
    for message in messages:
        if not isinstance(message, dict): continue
        role = labels.get(str(message.get("role")), str(message.get("role") or "Message"))
        name = f" ({message['name']})" if message.get("name") else ""
        call_id = f" [call_id={message['tool_call_id']}]" if message.get("tool_call_id") else ""
        sections: list[str] = []
        content = normalize_content(message.get("content"))
        if content: sections.append(content)
        if isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
            sections.append("[Client tool calls]\n" + json.dumps(message["tool_calls"], ensure_ascii=False, separators=(",", ":")))
        if message.get("function_call"):
            sections.append("[Client function call]\n" + json.dumps(message["function_call"], ensure_ascii=False, separators=(",", ":")))
        rendered.append(f"{role}{name}{call_id}:\n" + "\n".join(sections))
    return apply_response_format("\n\n".join(rendered) + "\n\nAnswer the latest user message.", request.get("response_format"))


def anthropic_messages_to_prompt(body: dict[str, Any]) -> str:
    messages: list[dict[str, Any]] = []
    system = normalize_content(body.get("system")).strip()
    if system: messages.append({"role": "system", "content": system})
    source = body.get("messages")
    if not isinstance(source, list) or not source: raise ProxyError(400, "invalid_request_error", "messages must be a non-empty array.")
    for message in source:
        if not isinstance(message, dict): continue
        messages.append({"role": "assistant" if message.get("role") == "assistant" else "user", "content": message.get("content", "")})
    return messages_to_prompt(messages)


def responses_to_prompt(body: dict[str, Any]) -> str:
    source = body.get("input")
    if isinstance(source, str): prompt = source
    elif isinstance(source, list):
        messages = []
        call_names: dict[str, str] = {}
        for item in source:
            if isinstance(item, str): messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                typ = str(item.get("type") or "")
                call_id = str(item.get("call_id") or "").strip()
                if typ in {"custom_tool_call", "function_call"}:
                    name = str(item.get("name") or "").strip()
                    if call_id and name: call_names[call_id] = name
                    if typ == "custom_tool_call":
                        call = {"id": call_id, "type": "custom", "custom": {"name": name, "input": item.get("input", "")}}
                    else:
                        call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": item.get("arguments", "")}}
                    messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
                elif typ in {"custom_tool_call_output", "function_call_output"}:
                    messages.append({
                        "role": "tool",
                        "content": item.get("output", ""),
                        "name": item.get("name") or call_names.get(call_id),
                        "tool_call_id": call_id,
                    })
                else:
                    messages.append({"role": item.get("role", "user"), "content": item.get("content", item.get("text", "")), "name": item.get("name")})
        prompt = messages_to_prompt(messages or [{"role": "user", "content": ""}], body)
    else: prompt = normalize_content(source)
    instructions = normalize_content(body.get("instructions")).strip()
    if instructions: prompt = f"System:\n{instructions}\n\nUser:\n{prompt}"
    return apply_response_format(prompt, body.get("text", {}).get("format") if isinstance(body.get("text"), dict) else body.get("response_format"))


def truncate_at_stop(text: str, stop: Any) -> str:
    stops = stop if isinstance(stop, list) else ([stop] if isinstance(stop, str) else [])
    end = len(text)
    for item in stops:
        if not item: continue
        index = text.find(str(item))
        if 0 <= index < end: end = index
    return text[:end]


def limit_anthropic_output(text: str, stop_sequences: Any, max_tokens: Any) -> tuple[str, str, Optional[str]]:
    value = str(text or ""); stop_reason = "end_turn"; stop_sequence = None; end = len(value)
    for item in stop_sequences if isinstance(stop_sequences, list) else []:
        if not isinstance(item, str) or not item: continue
        index = value.find(item)
        if 0 <= index < end: end = index; stop_reason = "stop_sequence"; stop_sequence = item
    value = value[:end]
    try: maximum = int(max_tokens or 0)
    except Exception: maximum = 0
    if maximum > 0 and estimate_tokens(value) > maximum:
        ratio = maximum / max(1, estimate_tokens(value)); value = value[:max(1, int(len(value) * ratio))]
        stop_reason = "max_tokens"; stop_sequence = None
    return value, stop_reason, stop_sequence


def openai_usage(prompt: str, output: str, usage: Any) -> dict[str, int]:
    if isinstance(usage, dict):
        p = int(usage.get("prompt_tokens") or 0); c = int(usage.get("completion_tokens") or 0)
        return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": int(usage.get("total_tokens") or p + c)}
    return usage_for(prompt, output)


def anthropic_usage(prompt: str, output: str, usage: Any) -> dict[str, int]:
    value = openai_usage(prompt, output, usage)
    return {"input_tokens": value["prompt_tokens"], "output_tokens": value["completion_tokens"]}


def responses_usage(prompt: str, output: str, usage: Any) -> dict[str, int]:
    value = openai_usage(prompt, output, usage)
    return {"input_tokens": value["prompt_tokens"], "output_tokens": value["completion_tokens"], "total_tokens": value["total_tokens"]}


def wants_workbuddy_events(headers: Any, body: dict[str, Any], profile: str = "agent") -> bool:
    if profile != "agent": return False
    enabled = {"1", "true", "yes", "on"}
    header = str(headers.get("X-WorkBuddy-Events", "")).strip().lower()
    field = str(body.get("workbuddy_events", "")).strip().lower()
    return body.get("workbuddy_events") is True or header in enabled or field in enabled


def conversation_id(headers: Any, body: dict[str, Any]) -> str:
    candidate = str(headers.get("X-Conversation-ID") or body.get("conversation_id") or body.get("user") or "").strip()
    return f"openai-{slug(candidate)}-{uid()[:8]}" if candidate else "openai-" + uid()


class ProxyApplication:
    def __init__(self, config: Config):
        self.config = config; self.gateways = GatewayManager(config); self.started_at = time.time()
        self.mcp_admin = McpAdminManager(config.mcp_config, config.mcp_config_source, config.mcp_server_sources,
                                         config.mcp_admin_timeout_ms, config.mcp_admin_cache_ms)
        self.usage_ledger = UsageLedger(RUNTIME_DIR / "usage-ledger.json", config.usage_ledger_max_records)
        self.account_client = WorkBuddyAccountClient(config)
        self.mcp_lock = threading.RLock(); self.last_mcp_refresh_ms = 0.0; self.server: Optional[ThreadingHTTPServer] = None

    def record_usage(self, model: str, protocol: str, usage: Any, source_usage: Any) -> None:
        self.usage_ledger.append(model, protocol, usage, not isinstance(source_usage, dict))

    def model_rates(self) -> dict[str, Any]:
        self.config.refresh_catalog(False)
        data: list[dict[str, Any]] = []
        for model_id in self.config.models:
            item = self.config.model_catalog.get(model_id, {})
            data.append({
                "id": model_id,
                "name": item.get("name", model_id),
                "vendor": item.get("vendor"),
                "type": item.get("type", "chat"),
                "credits": item.get("credits"),
                "supports_tool_call": bool(item.get("supportsToolCall")),
                "supports_images": bool(item.get("supportsImages")),
                "supports_reasoning": bool(item.get("supportsReasoning")),
                "max_input_tokens": item.get("maxInputTokens"),
                "max_output_tokens": item.get("maxOutputTokens"),
            })
        return {"object": "list", "source": self.config.model_catalog_source,
                "updated_at": self.config.model_catalog_updated_at, "data": data}

    def dashboard(self) -> dict[str, Any]:
        def account_snapshot(loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
            try:
                return loader()
            except ProxyError as exc:
                return {"available": False, "error": str(exc)}
        self.config.refresh_catalog(False)
        usage = self.usage_ledger.snapshot(100)
        return {
            "generated_at": now_iso(),
            "service": {
                "status": "ok", "uptime_seconds": round(time.time() - self.started_at, 3),
                "model_count": len(self.config.models), "default_model": self.config.default_model,
                "model_catalog_source": self.config.model_catalog_source,
            },
            "checkin": account_snapshot(self.account_client.checkin),
            "account": account_snapshot(self.account_client.account),
            "usage": usage,
        }

    def refresh_mcp(self, force: bool = False, restart: bool = True) -> dict[str, Any]:
        with self.mcp_lock:
            current = time.time() * 1000
            if not force and current - self.last_mcp_refresh_ms < self.config.mcp_refresh_ms:
                return {"checked": False, "changed": False, "restartedGateways": 0, **self.mcp_admin.summary()}
            self.last_mcp_refresh_ms = current
            explicit = self.config.mcp_config if self.config.mcp_config_source == "explicit" else os.getenv("WORKBUDDY_MCP_CONFIG", "")
            value, source, sources = resolve_mcp_configuration(explicit, Path.home() / ".workbuddy" / ".mcp.json")
            changed = self.mcp_admin.update(value, source, sources)
            self.config.mcp_config, self.config.mcp_config_source, self.config.mcp_server_sources = value, source, sources
            restarted = self.gateways.stop_profile("agent") if restart and (changed or force) else 0
            if changed: log("mcp", f"configuration refreshed source={source} servers={self.mcp_admin.summary()['server_count']}")
            self.write_runtime_state()
            return {"checked": True, "changed": changed, "restartedGateways": restarted, **self.mcp_admin.summary()}

    def state(self) -> dict[str, Any]:
        return {"app": APP_NAME, "version": VERSION, "pid": os.getpid(), "host": self.config.host, "port": self.config.port,
                "base_url": f"http://{self.config.host}:{self.config.port}", "started_at": _dt.datetime.fromtimestamp(self.started_at, _dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "uptime_seconds": round(time.time() - self.started_at, 3), "default_model": self.config.default_model,
                "models": self.config.models, "model_count": len(self.config.models), "model_catalog_source": self.config.model_catalog_source,
                "request_timeout_ms": self.config.request_timeout_ms, "sse_heartbeat_ms": self.config.sse_heartbeat_ms,
                "mcp": self.mcp_admin.summary(), "gateways": self.gateways.list(), "python": sys.version.split()[0]}

    def write_runtime_state(self) -> None:
        with contextlib.suppress(Exception): STATE_FILE.write_text(json.dumps(self.state(), ensure_ascii=False, indent=2), encoding="utf-8")

    def shutdown(self) -> None:
        self.gateways.stop_all()
        with contextlib.suppress(Exception): STATE_FILE.unlink()
        if self.server: self.server.shutdown()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, **kwargs: Any):
        self._sse_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    @property
    def app(self) -> ProxyApplication:
        return getattr(self.server, "app")

    def log_message(self, fmt: str, *args: Any) -> None:
        log("http", f"{self.client_address[0]} {fmt % args}")

    def common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Conversation-ID, X-Api-Key, X-WorkBuddy-Events, Anthropic-Version, Anthropic-Beta")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("X-Content-Type-Options", "nosniff")

    def send_json(self, status: int, value: Any, headers: Optional[dict[str, str]] = None) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._response_started = True; self.send_response(status); self.common_headers(); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store")
        for key, val in (headers or {}).items(): self.send_header(key, str(val))
        self.end_headers(); self.wfile.write(payload)

    def send_html(self, status: int, value: str) -> None:
        payload = value.encode("utf-8")
        self._response_started = True; self.send_response(status); self.common_headers(); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(payload)

    def begin_sse(self, headers: Optional[dict[str, str]] = None) -> None:
        self._response_started = True; self.send_response(200); self.common_headers(); self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform"); self.send_header("Connection", "close"); self.send_header("X-Accel-Buffering", "no")
        for key, val in (headers or {}).items(): self.send_header(key, str(val))
        self.end_headers()

    def write_sse(self, data: Any, event: Optional[str] = None) -> None:
        raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        frame = (f"event: {event}\n" if event else "") + f"data: {raw}\n\n"
        with self._sse_lock:
            self.wfile.write(frame.encode("utf-8")); self.wfile.flush()

    @contextlib.contextmanager
    def sse_heartbeat(self):
        """Keep an active streaming response alive while WorkBuddy is thinking."""
        stopped = threading.Event()
        interval = max(1000, self.app.config.sse_heartbeat_ms) / 1000

        def beat() -> None:
            while not stopped.wait(interval):
                try:
                    self.write_sse({"type": "response.heartbeat"}, "response.heartbeat")
                except (OSError, ValueError):
                    return

        worker = threading.Thread(target=beat, name="workbuddy-sse-heartbeat", daemon=True)
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join(timeout=1)

    def read_json(self, limit: int = 10 * 1024 * 1024) -> dict[str, Any]:
        try: size = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError: size = 0
        if size > limit: raise ProxyError(413, "request_too_large", f"Request body exceeds {limit} bytes.")
        raw = self.rfile.read(size) if size else b""
        if not raw: return {}
        try:
            value = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(value, dict): raise ValueError("top-level JSON must be an object")
            return value
        except Exception as exc: raise ProxyError(400, "invalid_json", "Request body must be valid JSON.") from exc

    def check_auth(self) -> None:
        key = self.app.config.api_key
        if not key: return
        auth = self.headers.get("Authorization", ""); x_key = self.headers.get("X-Api-Key", "")
        if auth != "Bearer " + key and x_key != key: raise ProxyError(401, "invalid_api_key", "API key mismatch.")

    def check_admin(self) -> None:
        if not is_loopback_host(self.client_address[0]): raise ProxyError(403, "admin_local_only", "Admin endpoints are local-only.")

    def openai_error(self, error: Exception) -> None:
        status = error.status if isinstance(error, ProxyError) else 500; code = error.code if isinstance(error, ProxyError) else "proxy_error"
        self.send_json(status, {"error": {"message": str(error), "type": "server_error" if status >= 500 else "invalid_request_error", "param": None, "code": code}})

    def anthropic_error(self, error: Exception) -> None:
        status = error.status if isinstance(error, ProxyError) else 500; typ = "invalid_request_error"
        if status in {401, 403}: typ = "authentication_error"
        elif status == 404: typ = "not_found_error"
        elif status == 429: typ = "rate_limit_error"
        elif status >= 500: typ = "api_error"
        self.send_json(status, {"type": "error", "error": {"type": typ, "message": str(error)}})

    def do_OPTIONS(self) -> None:
        self.send_response(204); self.common_headers(); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self) -> None:
        try: self.route_get()
        except Exception as exc:
            log("error", str(exc), traceback.format_exc())
            if not getattr(self, "_headers_buffer", None): return
            self.openai_error(exc)

    def do_POST(self) -> None:
        try: self.route_post()
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as exc:
            log("error", str(exc), traceback.format_exc())
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/v1/messages"): self.anthropic_error(exc)
            else: self.openai_error(exc)

    def route_get(self) -> None:
        parsed = urllib.parse.urlsplit(self.path); path = parsed.path
        if path in {"/", "/health", "/v1/health"}:
            self.app.config.refresh_catalog(False); self.app.write_runtime_state()
            self.send_json(200, {"status": "ok", **self.app.state(), "endpoints": ["/v1/models", "/v1/chat/completions", "/v1/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens", "/admin"]}); return
        if path == "/v1/models":
            self.check_auth(); self.app.config.refresh_catalog(False)
            created = int(time.time()); data = []
            for model_id in self.app.config.models:
                item = self.app.config.model_catalog.get(model_id, {}); data.append({"id": model_id, "object": "model", "created": created, "owned_by": "workbuddy", "name": item.get("name", model_id), "vendor": item.get("vendor"), "type": item.get("type", "chat"), "supports_tool_call": item.get("supportsToolCall", False), "supports_images": item.get("supportsImages", False), "supports_reasoning": item.get("supportsReasoning", False), "credits": item.get("credits"), "max_input_tokens": item.get("maxInputTokens"), "max_output_tokens": item.get("maxOutputTokens")})
            self.send_json(200, {"object": "list", "data": data, "has_more": False, "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None, "source": self.app.config.model_catalog_source, "updated_at": self.app.config.model_catalog_updated_at}); return
        if path.startswith("/v1/models/"):
            self.check_auth(); model_id = self.app.config.resolve_model(urllib.parse.unquote(path[len("/v1/models/"):]))
            item = self.app.config.model_catalog.get(model_id, {}); self.send_json(200, {"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "workbuddy", "name": item.get("name", model_id), "vendor": item.get("vendor"), "type": item.get("type", "chat"), "supports_tool_call": item.get("supportsToolCall", False), "supports_images": item.get("supportsImages", False), "supports_reasoning": item.get("supportsReasoning", False), "credits": item.get("credits"), "max_input_tokens": item.get("maxInputTokens"), "max_output_tokens": item.get("maxOutputTokens")}); return
        if path == "/admin":
            self.check_admin(); html_file = ROOT / "admin.html"
            html = html_file.read_text(encoding="utf-8") if html_file.exists() else "<!doctype html><meta charset=utf-8><title>workbuddy_to_api</title><h1>workbuddy_to_api</h1><p>Use /admin/mcp/servers and /admin/mcp/tools.</p>"
            self.send_html(200, html); return
        if path == "/admin/dashboard":
            self.check_admin(); self.check_auth(); self.send_json(200, self.app.dashboard()); return
        if path == "/admin/account":
            self.check_admin(); self.check_auth(); self.send_json(200, self.app.account_client.account()); return
        if path == "/admin/checkin":
            self.check_admin(); self.check_auth(); self.send_json(200, self.app.account_client.checkin()); return
        if path == "/admin/usage":
            self.check_admin(); self.check_auth(); query = urllib.parse.parse_qs(parsed.query)
            try: limit = int((query.get("limit") or ["100"])[0])
            except (TypeError, ValueError): limit = 100
            self.send_json(200, self.app.usage_ledger.snapshot(limit)); return
        if path == "/admin/models/rates":
            self.check_admin(); self.check_auth(); self.send_json(200, self.app.model_rates()); return
        if path == "/admin/mcp/servers":
            self.check_admin(); self.check_auth(); self.app.refresh_mcp(False, False)
            self.send_json(200, {"object": "list", "summary": self.app.mcp_admin.summary(), "data": self.app.mcp_admin.list_servers()}); return
        if path == "/admin/mcp/tools":
            self.check_admin(); self.check_auth(); self.app.refresh_mcp(False, False); query = urllib.parse.parse_qs(parsed.query)
            self.send_json(200, self.app.mcp_admin.list_tools((query.get("server") or [""])[0], (query.get("refresh") or [""])[0].lower() in {"1", "true", "yes"})); return
        if path == "/admin/status":
            self.check_admin(); self.check_auth(); self.send_json(200, self.app.state()); return
        raise ProxyError(404, "not_found", "Endpoint not found.")

    def route_post(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/admin/mcp/reload":
            self.check_admin(); self.check_auth(); self.read_json(); result = self.app.refresh_mcp(True, True)
            self.send_json(200, {"status": "ok", "changed": result["changed"], "restarted_gateways": result["restartedGateways"], "summary": self.app.mcp_admin.summary(), "servers": self.app.mcp_admin.list_servers()}); return
        if path == "/admin/mcp/test":
            self.check_admin(); self.check_auth(); self.app.refresh_mcp(False, False); self.send_json(200, self.app.mcp_admin.test(self.read_json())); return
        if path == "/admin/checkin/claim":
            self.check_admin(); self.check_auth(); self.read_json(); self.send_json(200, self.app.account_client.claim_checkin()); return
        if path == "/admin/shutdown":
            self.check_admin(); self.check_auth(); self.read_json(); self.send_json(200, {"status": "stopping", "pid": os.getpid()})
            threading.Thread(target=self.app.shutdown, daemon=True).start(); return
        self.check_auth(); body = self.read_json()
        if path == "/v1/chat/completions": self.handle_chat(body); return
        if path == "/v1/responses": self.handle_responses(body); return
        if path == "/v1/completions": self.handle_completions(body); return
        if path == "/v1/messages/count_tokens": self.handle_anthropic_count(body); return
        if path == "/v1/messages": self.handle_anthropic(body); return
        raise ProxyError(404, "not_found", "Endpoint not found.")


    def handle_chat(self, body: dict[str, Any]) -> None:
        model = self.app.config.resolve_model(body.get("model")); base_prompt = messages_to_prompt(body.get("messages"), body)
        plan = make_tool_plan(body, "openai", base_prompt); stream = body.get("stream") is True
        event_mode = wants_workbuddy_events(self.headers, body, plan["profile"]); conv = conversation_id(self.headers, body)
        completion_id = "chatcmpl-" + uid(); created = int(time.time())
        if stream:
            self.begin_sse({"X-Usage-Estimated": "false" if event_mode else "true"})
            self.write_sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                            "system_fingerprint": "workbuddy-python-" + VERSION,
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "logprobs": None, "finish_reason": None}]})
            def delta(value: str, _: Any) -> None:
                if not plan["enabled"]:
                    self.write_sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                    "choices": [{"index": 0, "delta": {"content": value}, "logprobs": None, "finish_reason": None}]})
            def event(value: dict[str, Any]) -> None: self.write_sse(value, value.get("type", "workbuddy.event"))
            generation = consume_generation(self.app, model, plan["prompt"], conv, plan["profile"], event_mode, delta, event)
            finish = "stop"
            if plan["enabled"]:
                try:
                    generation, parsed = resolve_client_tool_output(self.app, model, plan, conv, generation)
                except ProxyError as exc:
                    self.write_sse({"error": {"message": str(exc), "type": "server_error", "param": None, "code": exc.code}})
                    self.write_sse("[DONE]"); self.close_connection = True; return
                if parsed["type"] == "tool_calls":
                    output = parsed["commentary"]
                    if output:
                        self.write_sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                        "choices": [{"index": 0, "delta": {"content": output}, "logprobs": None, "finish_reason": None}]})
                    calls = assign_call_ids(parsed["calls"], "call"); finish = "tool_calls"
                    for index, call in enumerate(calls):
                        self.write_sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                                        "choices": [{"index": 0, "delta": {"tool_calls": [{"index": index, "id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))}}]}, "logprobs": None, "finish_reason": None}]})
                else:
                    output = truncate_at_stop(parsed["content"], body.get("stop"))
                    if output: self.write_sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": output}, "logprobs": None, "finish_reason": None}]})
            else:
                output = truncate_at_stop(generation["text"], body.get("stop"))
            self.write_sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                            "choices": [{"index": 0, "delta": {}, "logprobs": None, "finish_reason": finish}]})
            usage = openai_usage(base_prompt, output, generation.get("usage"))
            if isinstance(body.get("stream_options"), dict) and body["stream_options"].get("include_usage"):
                self.write_sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [], "usage": usage})
            self.app.record_usage(model, "chat", usage, generation.get("usage"))
            self.write_sse("[DONE]"); self.close_connection = True; return

        generation = consume_generation(self.app, model, plan["prompt"], conv, plan["profile"], event_mode)
        finish = "stop"; output = truncate_at_stop(generation["text"], body.get("stop"))
        message: dict[str, Any] = {"role": "assistant", "content": output, "refusal": None}
        if plan["enabled"]:
            generation, parsed = resolve_client_tool_output(self.app, model, plan, conv, generation)
            if parsed["type"] == "tool_calls":
                output = parsed["commentary"]
                calls = assign_call_ids(parsed["calls"], "call"); finish = "tool_calls"; message["content"] = output or None
                message["tool_calls"] = [{"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))}} for call in calls]
            else: message["content"] = output = truncate_at_stop(parsed["content"], body.get("stop"))
        usage = openai_usage(base_prompt, output, generation.get("usage"))
        payload: dict[str, Any] = {"id": completion_id, "object": "chat.completion", "created": created, "model": model,
                                   "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": finish}],
                                   "usage": usage, "system_fingerprint": "workbuddy-python-" + VERSION}
        if event_mode: payload.update({"workbuddy_events": generation.get("events", []), "workbuddy_usage": generation.get("usage")})
        self.app.record_usage(model, "chat", usage, generation.get("usage"))
        self.send_json(200, payload, {"X-Usage-Estimated": "false" if generation.get("usage") else "true"})

    def handle_anthropic_count(self, body: dict[str, Any]) -> None:
        prompt = anthropic_messages_to_prompt(body); self.send_json(200, {"input_tokens": estimate_tokens(prompt)})

    def handle_anthropic(self, body: dict[str, Any]) -> None:
        model = self.app.config.resolve_model(body.get("model")); base_prompt = anthropic_messages_to_prompt(body)
        plan = make_tool_plan(body, "anthropic", base_prompt); stream = body.get("stream") is True
        event_mode = wants_workbuddy_events(self.headers, body, plan["profile"]); conv = conversation_id(self.headers, body); message_id = "msg_" + uid()
        if stream:
            self.begin_sse({"X-Usage-Estimated": "false" if event_mode else "true"})
            self.write_sse({"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "content": [], "model": model, "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": estimate_tokens(base_prompt), "output_tokens": 0}}}, "message_start")
            self.write_sse({"type": "ping"}, "ping"); block_started = False
            def delta(value: str, _: Any) -> None:
                nonlocal block_started
                if plan["enabled"]: return
                if not block_started:
                    self.write_sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start"); block_started = True
                self.write_sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": value}}, "content_block_delta")
            def event(value: dict[str, Any]) -> None: self.write_sse(value, value.get("type", "workbuddy.event"))
            generation = consume_generation(self.app, model, plan["prompt"], conv, plan["profile"], event_mode, delta, event)
            if plan["enabled"]:
                try:
                    generation, parsed = resolve_client_tool_output(self.app, model, plan, conv, generation)
                except ProxyError as exc:
                    self.write_sse({"type": "error", "error": {"type": "api_error", "message": str(exc)}}, "error")
                    self.close_connection = True; return
                if parsed["type"] == "tool_calls":
                    raw = parsed["commentary"]; stop_reason = "tool_use"; stop_sequence = None; offset = 0
                    if raw:
                        self.write_sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start")
                        self.write_sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": raw}}, "content_block_delta")
                        self.write_sse({"type": "content_block_stop", "index": 0}, "content_block_stop"); offset = 1
                    for index, call in enumerate(assign_call_ids(parsed["calls"], "toolu"), start=offset):
                        self.write_sse({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": call["id"], "name": call["name"], "input": {}}}, "content_block_start")
                        self.write_sse({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))}}, "content_block_delta")
                        self.write_sse({"type": "content_block_stop", "index": index}, "content_block_stop")
                else:
                    raw, stop_reason, stop_sequence = limit_anthropic_output(parsed["content"], body.get("stop_sequences"), body.get("max_tokens"))
                    self.write_sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start")
                    self.write_sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": raw}}, "content_block_delta")
                    self.write_sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
            else:
                raw, stop_reason, stop_sequence = limit_anthropic_output(generation["text"], body.get("stop_sequences"), body.get("max_tokens"))
                if block_started: self.write_sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
            usage = anthropic_usage(base_prompt, raw, generation.get("usage"))
            self.app.record_usage(model, "anthropic", usage, generation.get("usage"))
            self.write_sse({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence}, "usage": {"output_tokens": usage["output_tokens"]}}, "message_delta")
            self.write_sse({"type": "message_stop"}, "message_stop"); self.close_connection = True; return

        generation = consume_generation(self.app, model, plan["prompt"], conv, plan["profile"], event_mode)
        output, stop_reason, stop_sequence = limit_anthropic_output(generation["text"], body.get("stop_sequences"), body.get("max_tokens"))
        content: list[dict[str, Any]] = [{"type": "text", "text": output}]
        if plan["enabled"]:
            generation, parsed = resolve_client_tool_output(self.app, model, plan, conv, generation)
            if parsed["type"] == "tool_calls":
                output = parsed["commentary"]; stop_reason = "tool_use"; stop_sequence = None
                content = ([{"type": "text", "text": output}] if output else []) + [{"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]} for call in assign_call_ids(parsed["calls"], "toolu")]
            else:
                output, stop_reason, stop_sequence = limit_anthropic_output(parsed["content"], body.get("stop_sequences"), body.get("max_tokens"))
                content = [{"type": "text", "text": output}]
        usage = anthropic_usage(base_prompt, output, generation.get("usage"))
        payload: dict[str, Any] = {"id": message_id, "type": "message", "role": "assistant", "content": content, "model": model,
                                   "stop_reason": stop_reason, "stop_sequence": stop_sequence, "usage": usage}
        if event_mode: payload.update({"workbuddy_events": generation.get("events", []), "workbuddy_usage": generation.get("usage")})
        self.app.record_usage(model, "anthropic", usage, generation.get("usage"))
        self.send_json(200, payload, {"X-Usage-Estimated": "false" if generation.get("usage") else "true"})


    def handle_responses(self, body: dict[str, Any]) -> None:
        model = self.app.config.resolve_model(body.get("model")); base_prompt = responses_to_prompt(body)
        plan = make_tool_plan(body, "responses", base_prompt); stream = body.get("stream") is True
        event_mode = wants_workbuddy_events(self.headers, body, plan["profile"]); conv = conversation_id(self.headers, body)
        response_id = "resp_" + uid(); created = int(time.time())
        if stream:
            self.begin_sse({"X-Usage-Estimated": "false" if event_mode else "true"})
            shell = {"id": response_id, "object": "response", "created_at": created, "status": "in_progress", "error": None,
                     "incomplete_details": None, "instructions": body.get("instructions"), "model": model, "output": [], "output_text": ""}
            self.write_sse({"type": "response.created", "response": shell}, "response.created")
            item_id = "msg_" + uid(); started = False
            def delta(value: str, _: Any) -> None:
                nonlocal started
                if plan["enabled"]: return
                if not started:
                    self.write_sse({"type": "response.output_item.added", "output_index": 0, "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}}, "response.output_item.added")
                    self.write_sse({"type": "response.content_part.added", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}, "response.content_part.added"); started = True
                self.write_sse({"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": value}, "response.output_text.delta")
            def event(value: dict[str, Any]) -> None: self.write_sse(value, value.get("type", "workbuddy.event"))
            try:
                with self.sse_heartbeat():
                    generation = consume_generation(self.app, model, plan["prompt"], conv, plan["profile"], event_mode, delta, event)
                    if plan["enabled"]:
                        generation, parsed = resolve_client_tool_output(self.app, model, plan, conv, generation)
            except ProxyError as exc:
                error = {"code": exc.code, "message": str(exc)}
                failed = {**shell, "status": "failed", "error": error, "output": [], "output_text": ""}
                self.write_sse({"type": "error", "code": exc.code, "message": str(exc), "param": None}, "error")
                self.write_sse({"type": "response.failed", "response": failed}, "response.failed")
                self.close_connection = True; return
            output = generation["text"]; items: list[dict[str, Any]] = []
            if plan["enabled"]:
                if parsed["type"] == "tool_calls":
                    output = parsed["commentary"]; offset = 0
                    if output:
                        item = {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
                        self.write_sse({"type": "response.output_item.added", "output_index": 0, "item": item}, "response.output_item.added")
                        self.write_sse({"type": "response.content_part.added", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}, "response.content_part.added")
                        self.write_sse({"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": output}, "response.output_text.delta")
                        self.write_sse({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": output}, "response.output_text.done")
                        item["status"] = "completed"; item["content"] = [{"type": "output_text", "text": output, "annotations": []}]; items.append(item)
                        self.write_sse({"type": "response.content_part.done", "item_id": item_id, "output_index": 0, "content_index": 0, "part": item["content"][0]}, "response.content_part.done")
                        self.write_sse({"type": "response.output_item.done", "output_index": 0, "item": item}, "response.output_item.done"); offset = 1
                    for index, call in enumerate(assign_call_ids(parsed["calls"], "fc"), start=offset):
                        arguments = json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))
                        item = {"id": call["id"], "type": "function_call", "status": "in_progress", "call_id": call["id"], "name": call["name"], "arguments": ""}
                        self.write_sse({"type": "response.output_item.added", "output_index": index, "item": item}, "response.output_item.added")
                        self.write_sse({"type": "response.function_call_arguments.delta", "item_id": call["id"], "output_index": index, "content_index": 0, "delta": arguments}, "response.function_call_arguments.delta")
                        self.write_sse({"type": "response.function_call_arguments.done", "item_id": call["id"], "output_index": index, "name": call["name"], "arguments": arguments}, "response.function_call_arguments.done")
                        item["status"] = "completed"; item["arguments"] = arguments; items.append(item)
                        self.write_sse({"type": "response.output_item.done", "output_index": index, "item": item}, "response.output_item.done")
                else: output = parsed["content"]
            if output and not started and (not plan["enabled"] or parsed["type"] == "final"):
                item = {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
                self.write_sse({"type": "response.output_item.added", "output_index": 0, "item": item}, "response.output_item.added")
                self.write_sse({"type": "response.content_part.added", "item_id": item_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}, "response.content_part.added")
                self.write_sse({"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": output}, "response.output_text.delta")
                self.write_sse({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": output}, "response.output_text.done")
                item["status"] = "completed"; item["content"] = [{"type": "output_text", "text": output, "annotations": []}]; items = [item]
                self.write_sse({"type": "response.content_part.done", "item_id": item_id, "output_index": 0, "content_index": 0, "part": item["content"][0]}, "response.content_part.done")
                self.write_sse({"type": "response.output_item.done", "output_index": 0, "item": item}, "response.output_item.done")
            elif started:
                item = {"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output, "annotations": []}]}; items = [item]
                self.write_sse({"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": output}, "response.output_text.done")
                self.write_sse({"type": "response.content_part.done", "item_id": item_id, "output_index": 0, "content_index": 0, "part": item["content"][0]}, "response.content_part.done")
                self.write_sse({"type": "response.output_item.done", "output_index": 0, "item": item}, "response.output_item.done")
            usage = responses_usage(base_prompt, output, generation.get("usage"))
            completed = {**shell, "status": "completed", "output": items, "output_text": output, "usage": usage}
            if event_mode: completed.update({"workbuddy_events": generation.get("events", []), "workbuddy_usage": generation.get("usage")})
            self.app.record_usage(model, "responses", usage, generation.get("usage"))
            self.write_sse({"type": "response.completed", "response": completed}, "response.completed"); self.close_connection = True; return

        generation = consume_generation(self.app, model, plan["prompt"], conv, plan["profile"], event_mode)
        output = generation["text"]; items: list[dict[str, Any]]
        if plan["enabled"]:
            generation, parsed = resolve_client_tool_output(self.app, model, plan, conv, generation)
            if parsed["type"] == "tool_calls":
                calls = assign_call_ids(parsed["calls"], "fc"); output = parsed["commentary"]
                items = ([{"id": "msg_" + uid(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output, "annotations": []}]}] if output else [])
                items += [{"id": call["id"], "type": "function_call", "status": "completed", "call_id": call["id"], "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))} for call in calls]
            else: output = parsed["content"]; items = [{"id": "msg_" + uid(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output, "annotations": []}]}]
        else: items = [{"id": "msg_" + uid(), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": output, "annotations": []}]}]
        usage = responses_usage(base_prompt, output, generation.get("usage"))
        payload: dict[str, Any] = {"id": response_id, "object": "response", "created_at": created, "status": "completed", "error": None,
                                   "incomplete_details": None, "instructions": body.get("instructions"), "model": model, "output": items,
                                   "output_text": output, "usage": usage}
        if event_mode: payload.update({"workbuddy_events": generation.get("events", []), "workbuddy_usage": generation.get("usage")})
        self.app.record_usage(model, "responses", usage, generation.get("usage"))
        self.send_json(200, payload, {"X-Usage-Estimated": "false" if generation.get("usage") else "true"})

    def handle_completions(self, body: dict[str, Any]) -> None:
        model = self.app.config.resolve_model(body.get("model")); source = body.get("prompt", "")
        prompt = "\n".join(str(x) for x in source) if isinstance(source, list) else str(source)
        conv = conversation_id(self.headers, body); stream = body.get("stream") is True; completion_id = "cmpl-" + uid(); created = int(time.time())
        if stream:
            self.begin_sse({"X-Usage-Estimated": "true"})
            def delta(value: str, _: Any) -> None:
                self.write_sse({"id": completion_id, "object": "text_completion", "created": created, "model": model,
                                "choices": [{"text": value, "index": 0, "logprobs": None, "finish_reason": None}]})
            generation = consume_generation(self.app, model, prompt, conv, "agent", False, delta)
            output = truncate_at_stop(generation["text"], body.get("stop"))
            usage = openai_usage(prompt, output, generation.get("usage"))
            self.write_sse({"id": completion_id, "object": "text_completion", "created": created, "model": model,
                            "choices": [{"text": "", "index": 0, "logprobs": None, "finish_reason": "stop"}]})
            self.app.record_usage(model, "completions", usage, generation.get("usage"))
            self.write_sse("[DONE]"); self.close_connection = True; return
        generation = consume_generation(self.app, model, prompt, conv)
        output = truncate_at_stop(generation["text"], body.get("stop"))
        usage = openai_usage(prompt, output, generation.get("usage"))
        self.app.record_usage(model, "completions", usage, generation.get("usage"))
        self.send_json(200, {"id": completion_id, "object": "text_completion", "created": created, "model": model,
                             "choices": [{"text": output, "index": 0, "logprobs": None, "finish_reason": "stop"}],
                             "usage": usage}, {"X-Usage-Estimated": "false" if generation.get("usage") else "true"})


def run_mcp_fixture() -> int:
    for raw in sys.stdin:
        try: request = json.loads(raw)
        except Exception: continue
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or request.get("id") is None: continue
        request_id = request["id"]; method = request.get("method"); params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method == "initialize": result = {"protocolVersion": params.get("protocolVersion") or MCP_PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "workbuddy-to-api-fixture", "version": VERSION}}
        elif method == "ping": result = {}
        elif method == "tools/list": result = {"tools": [{"name": "proxy_echo", "description": "Return a deterministic echo marker for proxy MCP verification.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "Text to echo."}}, "required": ["text"], "additionalProperties": False}}]}
        elif method == "tools/call":
            if params.get("name") != "proxy_echo":
                print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unknown tool: " + str(params.get("name"))}}, ensure_ascii=False), flush=True); continue
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}; text = str(args.get("text", ""))
            result = {"content": [{"type": "text", "text": "MCP_ECHO_OK:" + text}], "structuredContent": {"ok": True, "echo": text}, "isError": False}
        elif method == "resources/list": result = {"resources": []}
        elif method == "prompts/list": result = {"prompts": []}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found: " + str(method)}}, ensure_ascii=False), flush=True); continue
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


def process_exists(pid: int) -> bool:
    if pid <= 0: return False
    try:
        if os.name == "nt":
            result = subprocess.run(["powershell", "-NoProfile", "-Command", f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return result.returncode == 0
        os.kill(pid, 0); return True
    except Exception: return False


def read_state() -> Optional[dict[str, Any]]:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8")); return value if isinstance(value, dict) else None
    except Exception: return None


def stop_from_state(api_key: str = "") -> int:
    state = read_state()
    if not state:
        print("当前没有代理状态文件。"); return 0
    host = str(state.get("host") or "127.0.0.1"); port = int(state.get("port") or 3000); pid = int(state.get("pid") or 0)
    headers = {"Content-Type": "application/json"}
    if api_key: headers.update({"Authorization": "Bearer " + api_key, "X-Api-Key": api_key})
    with contextlib.suppress(Exception):
        response = urlopen_request(f"http://{host}:{port}/admin/shutdown", "POST", headers, {}, 5); response.close()
    for _ in range(40):
        if not process_exists(pid): break
        time.sleep(.2)
    if not process_exists(pid):
        with contextlib.suppress(Exception): STATE_FILE.unlink()
        print("workbuddy_to_api 已停止。"); return 0
    print(f"进程仍在运行，PID={pid}。"); return 1


def show_status(api_key: str = "") -> int:
    state = read_state()
    if not state:
        print("状态：未运行"); return 1
    pid = int(state.get("pid") or 0); alive = process_exists(pid)
    print(json.dumps({**state, "process_alive": alive}, ensure_ascii=False, indent=2)); return 0 if alive else 1



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Expose local WorkBuddy models through OpenAI and Anthropic compatible APIs.")
    parser.add_argument("--host", help="HTTP listen host")
    parser.add_argument("--port", type=int, help="HTTP listen port")
    parser.add_argument("--api-key", default=None, help="API key accepted as Bearer or x-api-key")
    parser.add_argument("--model", help="Default WorkBuddy model")
    parser.add_argument("--cwd", help="Working directory supplied to WorkBuddy")
    parser.add_argument("--workbuddy-exe", help="Path to WorkBuddy.exe")
    parser.add_argument("--cli-script", help="Path to WorkBuddy CLI script")
    parser.add_argument("--disable-tools", action="store_true", help="Disable WorkBuddy internal tools")
    parser.add_argument("--tools", help="WorkBuddy tool preset")
    parser.add_argument("--permission-mode", help="WorkBuddy permission mode")
    parser.add_argument("--max-turns", type=int, help="Maximum WorkBuddy turns")
    parser.add_argument("--mcp-config", default=None, help="MCP JSON string or config path")
    parser.add_argument("--mcp-refresh-ms", type=int, help="MCP discovery refresh interval")
    parser.add_argument("--event-max-bytes", type=int, help="Maximum bytes in one WorkBuddy event value")
    parser.add_argument("--state-file", help="Override runtime state file path")
    parser.add_argument("--background", action="store_true", help="Start as a detached background process")
    parser.add_argument("--stop", action="store_true", help="Stop the process recorded in runtime state")
    parser.add_argument("--status", action="store_true", help="Print runtime status")
    parser.add_argument("--mcp-fixture", action="store_true", help="Run the bundled stdio MCP echo fixture")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_background(raw_args: list[str]) -> int:
    state = read_state()
    if state and process_exists(int(state.get("pid") or 0)):
        print(f"已有代理进程在运行，PID={state.get('pid')}，地址={state.get('base_url') or ('http://' + str(state.get('host')) + ':' + str(state.get('port')))}")
        return 1
    with contextlib.suppress(Exception): STATE_FILE.unlink()
    child_args = [x for x in raw_args if x != "--background"]
    command = [sys.executable, str(Path(__file__).resolve()), *child_args]
    out = open(RUNTIME_DIR / "proxy.out.log", "ab", buffering=0); err = open(RUNTIME_DIR / "proxy.err.log", "ab", buffering=0)
    kwargs: dict[str, Any] = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": err, "close_fds": True}
    if os.name == "nt": kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else: kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs); out.close(); err.close()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"后台进程启动结束，退出码={process.returncode}。日志：{RUNTIME_DIR / 'proxy.err.log'}"); return 1
        state = read_state()
        if state and int(state.get("pid") or 0) == process.pid:
            print(f"workbuddy_to_api 已在后台启动。PID={process.pid}，地址={state.get('base_url')}")
            return 0
        time.sleep(.2)
    print(f"后台进程已创建，PID={process.pid}。状态文件稍后写入：{STATE_FILE}"); return 0


def serve(config: Config) -> int:
    if not config.workbuddy_exe.exists(): raise FileNotFoundError(f"WorkBuddy executable not found: {config.workbuddy_exe}")
    if not config.cli_script.exists(): raise FileNotFoundError(f"WorkBuddy CLI script not found: {config.cli_script}")
    app = ProxyApplication(config); server = ReusableThreadingHTTPServer((config.host, config.port), ApiHandler); server.app = app; app.server = server
    app.write_runtime_state()
    log("server", f"{APP_NAME} {VERSION} listening on http://{config.host}:{config.port}")
    log("catalog", f"source={config.model_catalog_source} models={len(config.models)} default={config.default_model}")
    log("mcp", json.dumps(app.mcp_admin.summary(), ensure_ascii=False, separators=(",", ":")))

    stopping = threading.Event()
    def request_stop(signum: int, _frame: Any) -> None:
        if stopping.is_set(): return
        stopping.set(); log("server", f"received signal {signum}"); threading.Thread(target=app.shutdown, daemon=True).start()
    with contextlib.suppress(Exception): signal.signal(signal.SIGINT, request_stop)
    with contextlib.suppress(Exception): signal.signal(signal.SIGTERM, request_stop)
    try: server.serve_forever(poll_interval=.25)
    finally:
        app.gateways.stop_all(); server.server_close()
        with contextlib.suppress(Exception):
            state = read_state()
            if state and int(state.get("pid") or 0) == os.getpid(): STATE_FILE.unlink()
        log("server", "stopped")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv(ROOT / ".env")
    global STATE_FILE
    raw_args = list(sys.argv[1:] if argv is None else argv); parser = build_parser(); args = parser.parse_args(raw_args)
    if args.state_file: STATE_FILE = Path(args.state_file).expanduser().resolve()
    if args.mcp_fixture: return run_mcp_fixture()
    api_key = args.api_key if args.api_key is not None else os.getenv("PROXY_API_KEY", "")
    if args.stop: return stop_from_state(api_key)
    if args.status: return show_status(api_key)
    if args.background: return start_background(raw_args)
    config = Config.from_args(args)
    return serve(config)


if __name__ == "__main__":
    try: raise SystemExit(main())
    except KeyboardInterrupt: raise SystemExit(130)
    except Exception as exc:
        log("fatal", str(exc), traceback.format_exc()); raise SystemExit(1)
