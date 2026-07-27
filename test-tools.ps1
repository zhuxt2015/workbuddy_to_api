[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:3000',
    [string]$ApiKey = 'local',
    [string]$Model = 'auto',
    [switch]$InternalTools,
    [switch]$Mcp
)

$ErrorActionPreference = 'Stop'
$headers = @{ Authorization = "Bearer $ApiKey"; 'Content-Type' = 'application/json' }
$anthropicHeaders = @{ 'x-api-key' = $ApiKey; 'anthropic-version' = '2023-06-01'; 'Content-Type' = 'application/json' }
$schema = @{
    type = 'object'
    properties = @{ city = @{ type = 'string' } }
    required = @('city')
    additionalProperties = $false
}

Write-Host '1/3 OpenAI Chat Completions tool call'
$chatBody = @{
    model = $Model
    messages = @(@{ role = 'user'; content = 'Use lookup_weather for Beijing.' })
    tools = @(@{ type = 'function'; function = @{ name = 'lookup_weather'; description = 'Look up weather'; parameters = $schema } })
    tool_choice = @{ type = 'function'; function = @{ name = 'lookup_weather' } }
} | ConvertTo-Json -Depth 20
$chat = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/chat/completions" -Headers $headers -Body $chatBody
$chatCall = $chat.choices[0].message.tool_calls[0]
if ($chat.choices[0].finish_reason -ne 'tool_calls' -or $chatCall.function.name -ne 'lookup_weather') {
    throw "OpenAI Chat tool response mismatch: $($chat | ConvertTo-Json -Depth 20 -Compress)"
}
$chatArgs = $chatCall.function.arguments | ConvertFrom-Json
Write-Host "  OK call_id=$($chatCall.id) city=$($chatArgs.city)"

Write-Host '2/3 OpenAI Responses function call'
$responsesBody = @{
    model = $Model
    input = 'Use lookup_weather for Shanghai.'
    tools = @(@{ type = 'function'; name = 'lookup_weather'; description = 'Look up weather'; parameters = $schema })
    tool_choice = @{ type = 'function'; name = 'lookup_weather' }
} | ConvertTo-Json -Depth 20
$responses = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/responses" -Headers $headers -Body $responsesBody
$responseCall = @($responses.output | Where-Object type -eq 'function_call')[0]
if ($responseCall.name -ne 'lookup_weather' -or -not $responseCall.call_id) {
    throw "Responses tool response mismatch: $($responses | ConvertTo-Json -Depth 20 -Compress)"
}
$responseArgs = $responseCall.arguments | ConvertFrom-Json
Write-Host "  OK call_id=$($responseCall.call_id) city=$($responseArgs.city)"

Write-Host '3/3 Anthropic Messages tool use'
$anthropicBody = @{
    model = $Model
    max_tokens = 256
    messages = @(@{ role = 'user'; content = 'Use lookup_weather for Shenzhen.' })
    tools = @(@{ name = 'lookup_weather'; description = 'Look up weather'; input_schema = $schema })
    tool_choice = @{ type = 'tool'; name = 'lookup_weather' }
} | ConvertTo-Json -Depth 20
$anthropic = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/messages" -Headers $anthropicHeaders -Body $anthropicBody
$anthropicCall = @($anthropic.content | Where-Object type -eq 'tool_use')[0]
if ($anthropic.stop_reason -ne 'tool_use' -or $anthropicCall.name -ne 'lookup_weather') {
    throw "Anthropic tool response mismatch: $($anthropic | ConvertTo-Json -Depth 20 -Compress)"
}
Write-Host "  OK tool_use_id=$($anthropicCall.id) city=$($anthropicCall.input.city)"

if ($InternalTools) {
    Write-Host 'Internal WorkBuddy Glob tool'
    $body = @{
        model = $Model
        messages = @(@{ role = 'user'; content = 'Use the Glob tool to list the top-level files in the current working directory. Include server.js in the answer if present.' })
    } | ConvertTo-Json -Depth 10
    $internal = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/chat/completions" -Headers $headers -Body $body
    $text = [string]$internal.choices[0].message.content
    if ($text -notmatch 'server\.js') { throw "Glob verification mismatch: $text" }
    Write-Host '  OK server.js found through agent tool flow'
}

if ($Mcp) {
    Write-Host 'Configured MCP proxy_echo tool'
    $body = @{
        model = $Model
        messages = @(@{ role = 'user'; content = 'Call the MCP tool mcp__proxy-fixture__proxy_echo with text exactly HELLO. Return the tool result verbatim.' })
    } | ConvertTo-Json -Depth 10
    $mcpResult = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1/chat/completions" -Headers $headers -Body $body
    $text = [string]$mcpResult.choices[0].message.content
    if ($text -notmatch 'MCP_ECHO_OK:HELLO') { throw "MCP verification mismatch: $text" }
    Write-Host '  OK MCP_ECHO_OK:HELLO'
}

Write-Host 'All requested tool tests passed.'
