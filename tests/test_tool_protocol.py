import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import workbuddy_to_api as proxy


class ToolProtocolParsingTests(unittest.TestCase):
    def setUp(self):
        self.tools = [{
            "name": "exec",
            "description": "Run JavaScript",
            "input_schema": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
        }]
        self.auto = {"mode": "auto", "name": None, "parallel": True}

    def test_progress_text_is_not_treated_as_final(self):
        result = proxy.parse_client_tool_output(
            "我先看看这个 skill 的结构，再逐层读内容。",
            self.tools,
            self.auto,
        )

        self.assertEqual("invalid", result["type"])

    def test_only_explicit_final_envelope_is_final(self):
        result = proxy.parse_client_tool_output(
            json.dumps({"type": "final", "content": "分析完成"}, ensure_ascii=False),
            self.tools,
            self.auto,
        )

        self.assertEqual({"type": "final", "content": "分析完成"}, result)

    def test_required_tool_choice_rejects_final_envelope(self):
        required = {"mode": "required", "name": None, "parallel": True}
        result = proxy.parse_client_tool_output(
            json.dumps({"type": "final", "content": "跳过工具"}, ensure_ascii=False),
            self.tools,
            required,
        )

        self.assertEqual("invalid", result["type"])
        self.assertIn("requires a tool call", result["reason"])

    def test_tool_call_can_include_commentary(self):
        raw = json.dumps({
            "type": "tool_calls",
            "commentary": "我先读取目录结构。",
            "calls": [{"name": "exec", "arguments": {"input": "await inspect();"}}],
        }, ensure_ascii=False)

        result = proxy.parse_client_tool_output(raw, self.tools, self.auto)

        self.assertEqual("tool_calls", result["type"])
        self.assertEqual("我先读取目录结构。", result["commentary"])
        self.assertEqual("await inspect();", result["calls"][0]["arguments"]["input"])

    def test_protocol_prompt_forbids_commentary_only(self):
        prompt = proxy.build_client_tool_prompt("Do the task.", self.tools, self.auto)

        self.assertIn('"commentary":"OPTIONAL_PROGRESS_TEXT"', prompt)
        self.assertIn("Never return a progress update by itself", prompt)

    def test_relaxed_exec_recovery_supplies_empty_commentary(self):
        raw = '{"type":"tool_calls","calls":[{"name":"exec","arguments":{"input":"await tools.run("status")"}}]}'

        result = proxy.parse_client_tool_output(raw, self.tools, self.auto)

        self.assertEqual("tool_calls", result["type"])
        self.assertEqual("", result["commentary"])


class ToolProtocolRepairTests(unittest.TestCase):
    def setUp(self):
        tools = [{"name": "exec", "description": "Run", "input_schema": {"type": "object"}}]
        choice = {"mode": "auto", "name": None, "parallel": True}
        self.plan = {"tools": tools, "choice": choice, "profile": "protocol"}
        self.app = SimpleNamespace()

    @patch("workbuddy_to_api.consume_generation")
    def test_invalid_output_gets_one_repair_attempt(self, consume_generation):
        consume_generation.return_value = {
            "text": json.dumps({"type": "tool_calls", "calls": [{"name": "exec", "arguments": {}}]}),
            "events": [],
            "usage": None,
        }

        generation, parsed = proxy.resolve_client_tool_output(
            self.app,
            "hy4-preview",
            self.plan,
            "conversation-1",
            {"text": "我先检查。", "events": [], "usage": None},
        )

        self.assertEqual("tool_calls", parsed["type"])
        self.assertIs(generation, consume_generation.return_value)
        consume_generation.assert_called_once()
        self.assertEqual("protocol", consume_generation.call_args.args[4])

    @patch("workbuddy_to_api.consume_generation")
    def test_second_invalid_output_fails_closed(self, consume_generation):
        consume_generation.return_value = {"text": "还是只说我先检查。", "events": [], "usage": None}

        with self.assertRaises(proxy.ProxyError) as raised:
            proxy.resolve_client_tool_output(
                self.app,
                "hy4-preview",
                self.plan,
                "conversation-1",
                {"text": "我先检查。", "events": [], "usage": None},
            )

        self.assertEqual(502, raised.exception.status)
        self.assertEqual("tool_protocol_error", raised.exception.code)
        consume_generation.assert_called_once()


class ResponsesPromptTests(unittest.TestCase):
    def test_custom_tool_output_is_preserved_in_prompt(self):
        prompt = proxy.responses_to_prompt({
            "input": [
                {"type": "message", "role": "user", "content": "Inspect the skill."},
                {
                    "type": "custom_tool_call",
                    "call_id": "fc_123",
                    "name": "exec",
                    "input": "const r = await tools.exec_command({cmd: 'find skill'});",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "fc_123",
                    "output": [
                        {"type": "input_text", "text": "Script completed\n"},
                        {"type": "input_text", "text": "skill/SKILL.md\nskill/core.md\n"},
                    ],
                },
            ]
        })

        self.assertIn("User:\nInspect the skill.", prompt)
        self.assertIn('"type":"custom"', prompt)
        self.assertIn('"name":"exec"', prompt)
        self.assertIn("Tool result (exec) [call_id=fc_123]:", prompt)
        self.assertIn("skill/SKILL.md\nskill/core.md", prompt)

    def test_function_call_output_is_preserved_in_prompt(self):
        prompt = proxy.responses_to_prompt({
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_456",
                    "name": "lookup",
                    "arguments": '{"query":"status"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_456",
                    "output": "ready",
                },
            ]
        })

        self.assertIn('"type":"function"', prompt)
        self.assertIn('"arguments":"{\\"query\\":\\"status\\"}"', prompt)
        self.assertIn("Tool result (lookup) [call_id=call_456]:\nready", prompt)


if __name__ == "__main__":
    unittest.main()
