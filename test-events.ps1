[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:3000',
    [string]$ApiKey = 'local',
    [string]$Model = 'auto',
    [int]$TimeoutSec = 240
)

$ErrorActionPreference = 'Stop'
$headers = @{
    Authorization = "Bearer $ApiKey"
    'X-WorkBuddy-Events' = '1'
}
$body = @{
    model = $Model
    stream = $true
    stream_options = @{ include_usage = $true }
    workbuddy_events = $true
    messages = @(
        @{
            role = 'user'
            content = 'Use the Glob tool exactly once to find package.json in the current working directory, then reply exactly WORKBUDDY_EVENT_OK.'
        }
    )
} | ConvertTo-Json -Depth 12

$response = Invoke-WebRequest `
    -Uri "$($BaseUrl.TrimEnd('/'))/v1/chat/completions" `
    -Method Post `
    -Headers $headers `
    -ContentType 'application/json; charset=utf-8' `
    -Body $body `
    -TimeoutSec $TimeoutSec

$raw = [string]$response.Content
$assistantText = ''
foreach ($line in ($raw -split "`r?`n")) {
    if (-not $line.StartsWith('data: {')) { continue }
    try {
        $item = ($line.Substring(6) | ConvertFrom-Json -Depth 20)
        $delta = $item.choices[0].delta.content
        if ($null -ne $delta) { $assistantText += [string]$delta }
    } catch {}
}
$checks = [ordered]@{
    tool_call = $raw.Contains('event: workbuddy.tool_call')
    tool_result = $raw.Contains('event: workbuddy.tool_result')
    usage_event = $raw.Contains('event: workbuddy.usage')
    assistant_text = $assistantText.Contains('WORKBUDDY_EVENT_OK')
    done = $raw.Contains('data: [DONE]')
}

$checks.GetEnumerator() | ForEach-Object {
    Write-Host ("{0,-16} {1}" -f $_.Key, $(if ($_.Value) { 'OK' } else { 'MISSING' }))
}

if ($checks.Values -contains $false) {
    $preview = if ($raw.Length -gt 5000) { $raw.Substring(0, 5000) } else { $raw }
    Write-Host '--- response preview ---'
    Write-Host $preview
    throw 'WorkBuddy event stream test failed.'
}

Write-Host 'WorkBuddy event stream test passed.'
