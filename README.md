# workbuddy_to_api

把本机 WorkBuddy 的模型能力转换为 OpenAI 与 Anthropic 兼容 API。项目主体是一个 Python 单文件程序，使用 Python 标准库，无第三方运行依赖。

## 功能

- OpenAI Chat Completions：`/v1/chat/completions`
- OpenAI Responses：`/v1/responses`
- OpenAI Completions：`/v1/completions`
- Anthropic Messages：`/v1/messages`
- Anthropic Token Count：`/v1/messages/count_tokens`
- 动态读取 WorkBuddy 本地模型目录
- OpenAI / Anthropic 客户端函数调用兼容
- WorkBuddy 内置工具调用
- MCP 配置发现、工具清单、连通测试与管理页
- SSE 流式输出
- WorkBuddy 工具调用、工具结果、阶段与用量事件
- 后台启动、状态查看和停止

## 运行要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 已安装并登录 WorkBuddy
- 默认安装目录：`%LOCALAPPDATA%\Programs\WorkBuddy`

模型目录读取顺序：

1. `~/.workbuddy/local_storage/entry_*.info` 中的最新模型目录
2. WorkBuddy 安装目录中的 `cli/product.json`
3. 程序内置的基础模型列表

## 快速开始

```powershell
cd workbuddy_to_api
python .\workbuddy_to_api.py --background --api-key local
```

默认地址：

- API 根地址：`http://127.0.0.1:3000`
- OpenAI Base URL：`http://127.0.0.1:3000/v1`
- 管理页：`http://127.0.0.1:3000/admin`

### 前台启动

```powershell
python .\workbuddy_to_api.py --api-key local
```

### 查看状态

```powershell
python .\workbuddy_to_api.py --status
```

### 停止服务

```powershell
python .\workbuddy_to_api.py --stop --api-key local
```

运行日志和状态文件位于 `runtime/`。项目不包含 PowerShell 启动或测试包装脚本；服务管理统一通过 `workbuddy_to_api.py` 执行。

## OpenAI 客户端配置

| 配置项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:3000/v1` |
| API Key | `local` |
| Model | `auto` 或 `/v1/models` 返回的模型 ID |

Python OpenAI SDK 示例：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:3000/v1",
    api_key="local",
)

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "你好，请介绍一下你自己"}],
)
print(response.choices[0].message.content)
```

PowerShell 示例：

```powershell
$headers = @{ Authorization = 'Bearer local' }
$body = @{
    model = 'auto'
    messages = @(@{ role = 'user'; content = '只回复：OK' })
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
    -Method Post `
    -Uri 'http://127.0.0.1:3000/v1/chat/completions' `
    -Headers $headers `
    -ContentType 'application/json; charset=utf-8' `
    -Body $body
```

## Anthropic 客户端配置

| 配置项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:3000` |
| API Key | `local` |
| Model | `auto` |

Python Anthropic SDK 示例：

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:3000",
    api_key="local",
)

message = client.messages.create(
    model="auto",
    max_tokens=512,
    messages=[{"role": "user", "content": "你好"}],
)
print(message.content[0].text)
```

## 模型列表

```powershell
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:3000/v1/models' `
  -Headers @{ Authorization = 'Bearer local' }
```

模型别名可通过环境变量配置：

```dotenv
WORKBUDDY_MODEL_ALIASES={"my-default":"auto","claude-local":"glm-5.2"}
```

请求中的 `workbuddy/模型名` 和 `anthropic/模型名` 前缀会自动移除。常见 Claude 兼容模型名默认映射到 `WORKBUDDY_DEFAULT_MODEL`。

## 客户端函数调用

以下协议都支持客户端工具定义：

- OpenAI Chat Completions `tools`
- OpenAI Responses `tools`
- Anthropic Messages `tools`

代理会让 WorkBuddy 生成严格 JSON 工具选择结果，然后转换成各协议对应的 `tool_calls`、`function_call` 或 `tool_use`。

## WorkBuddy 内置工具与事件流

普通请求会启用 WorkBuddy 的默认工具。启动参数可控制工具配置：

```powershell
python .\workbuddy_to_api.py `
  --tools default `
  --permission-mode bypassPermissions `
  --max-turns 8
```

请求中设置以下任意一项，可在流式响应中接收 WorkBuddy 自定义事件：

- Header：`X-WorkBuddy-Events: 1`
- Body：`"workbuddy_events": true`

事件类型包括：

- `workbuddy.tool_call`
- `workbuddy.tool_result`
- `workbuddy.usage`
- `workbuddy.phase`
- `workbuddy.plan`
- `workbuddy.interruption`
- `workbuddy.session_end`

## MCP

默认配置发现顺序：

1. `WORKBUDDY_MCP_CONFIG`
2. `~/.workbuddy/.mcp.json`
3. 运行中的 WorkBuddy 本地 Connector 配置

用户配置与运行时 Connector 配置会按服务器名称合并。配置更新后，代理会刷新 MCP 清单并重启相关 agent gateway。

### MCP 管理接口

- `GET /admin/mcp/servers`
- `GET /admin/mcp/tools`
- `GET /admin/mcp/tools?server=SERVER&refresh=1`
- `POST /admin/mcp/reload`
- `POST /admin/mcp/test`

管理接口仅接受本机访问，并沿用代理 API Key。

`mcp-example.json` 包含一个 Python stdio 测试服务器。把其中的 `PATH_TO_PROJECT` 替换为项目绝对路径，然后启动：

```powershell
python .\workbuddy_to_api.py --mcp-config .\mcp-example.json --api-key local
```

## 环境变量

```powershell
Copy-Item .env.example .env
```

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `PROXY_HOST` | `127.0.0.1` | 监听地址 |
| `PROXY_PORT` | `3000` | 监听端口 |
| `PROXY_API_KEY` | 空 | Bearer / x-api-key |
| `WORKBUDDY_DEFAULT_MODEL` | `auto` | 默认模型 |
| `WORKBUDDY_CWD` | 当前目录 | WorkBuddy 工作目录 |
| `WORKBUDDY_MODELS` | 自动发现 | 逗号分隔模型列表 |
| `WORKBUDDY_DISABLE_TOOLS` | `0` | 设为 `1` 时关闭内置工具 |
| `WORKBUDDY_TOOLS` | `default` | WorkBuddy 工具预设 |
| `WORKBUDDY_MAX_TURNS` | `8` | 最大 agent 回合数 |
| `WORKBUDDY_MCP_CONFIG` | 自动发现 | MCP JSON 或配置文件路径 |
| `WORKBUDDY_EVENT_MAX_BYTES` | `65536` | 单个事件字段大小上限 |

完整配置见 `.env.example`。

## 命令行参数

```text
python workbuddy_to_api.py --help
```

常用参数：`--host`、`--port`、`--api-key`、`--model`、`--cwd`、`--background`、`--status`、`--stop`、`--disable-tools`、`--mcp-config`。

## 连通性检查

服务启动后，可用以下命令检查服务状态和模型列表：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:3000/health'
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:3000/v1/models' `
  -Headers @{ Authorization = 'Bearer local' }
```

## 安装为命令

```powershell
python -m pip install -e .
workbuddy-to-api --background --api-key local
```

## GitHub 上传

```powershell
git init
git add .
git commit -m "Initial Python release"
git branch -M main
git remote add origin YOUR_GITHUB_REPOSITORY
git push -u origin main
```

提交前检查 `.env`、`runtime/` 和 MCP 配置中的令牌信息。它们已在 `.gitignore` 中排除或应使用占位值。

## 常见问题

### WorkBuddy 路径识别异常

```powershell
python .\workbuddy_to_api.py `
  --workbuddy-exe "$env:LOCALAPPDATA\Programs\WorkBuddy\WorkBuddy.exe" `
  --cli-script "$env:LOCALAPPDATA\Programs\WorkBuddy\resources\app.asar.unpacked\cli\bin\codebuddy"
```

### 端口被占用

```powershell
python .\workbuddy_to_api.py --port 3010 --api-key local
```

客户端 Base URL 对应调整为 `http://127.0.0.1:3010/v1`。

### 查看日志

```powershell
Get-Content .\runtime\proxy.out.log -Tail 100
Get-Content .\runtime\proxy.err.log -Tail 100
Get-Content .\runtime\gateway-auto-agent.err.log -Tail 100
```

## 项目结构

```text
workbuddy_to_api/
├─ workbuddy_to_api.py       # Python 主程序与命令行入口
├─ admin.html                # MCP 管理页
├─ pyproject.toml            # Python 项目元数据
├─ .env.example              # 配置示例
├─ mcp-example.json          # MCP 测试配置
└─ runtime/                  # 运行时状态与日志（不纳入 Git）
```

## License

MIT
