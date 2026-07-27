[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:3000/v1',
    [string]$ApiKey = 'local',
    [string]$Model = 'auto',
    [switch]$Stream
)

$ErrorActionPreference = 'Stop'
$headers = @{ Authorization = "Bearer $ApiKey" }
Write-Host '=== Models ==='
(Invoke-RestMethod -Uri "$BaseUrl/models" -Headers $headers -TimeoutSec 10).data | Select-Object id,owned_by | Format-Table -AutoSize

$bodyObject = @{
    model = $Model
    messages = @(@{ role = 'user'; content = '只回复：WorkBuddy proxy OK' })
    stream = [bool]$Stream
}
$body = $bodyObject | ConvertTo-Json -Depth 10 -Compress

Write-Host '=== Chat Completion ==='
if ($Stream) {
    $temp = Join-Path $env:TEMP ("workbuddy-proxy-test-" + [guid]::NewGuid().ToString('N') + '.json')
    try {
        [IO.File]::WriteAllText($temp, $body, [Text.UTF8Encoding]::new($false))
        & curl.exe -N -sS "$BaseUrl/chat/completions" `
            -H "Authorization: Bearer $ApiKey" `
            -H 'Content-Type: application/json' `
            --data-binary "@$temp"
        Write-Host
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
} else {
    $response = Invoke-RestMethod -Method Post -Uri "$BaseUrl/chat/completions" -Headers $headers `
        -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 360
    $response | ConvertTo-Json -Depth 20
}
