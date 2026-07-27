[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:3000/v1',
    [string]$ApiKey = 'local'
)
$ErrorActionPreference = 'Stop'
$headers = @{ Authorization = "Bearer $ApiKey" }
$response = Invoke-RestMethod -Uri "$($BaseUrl.TrimEnd('/'))/models" -Headers $headers -TimeoutSec 15
@($response.data) |
    Sort-Object id |
    Select-Object id, name, vendor, type, supports_tool_call, supports_images, supports_reasoning, max_input_tokens, max_output_tokens |
    Format-Table -AutoSize
Write-Host "模型数量：$(@($response.data).Count)；来源：$($response.source)"
