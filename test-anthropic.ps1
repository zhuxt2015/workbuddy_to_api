[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:3000',
    [string]$ApiKey = 'local',
    [string]$Model = 'auto',
    [switch]$Stream
)

$ErrorActionPreference = 'Stop'
$BaseUrl = $BaseUrl.TrimEnd('/')
$headers = @{
    'x-api-key' = $ApiKey
    'anthropic-version' = '2023-06-01'
}

$bodyObject = @{
    model = $Model
    max_tokens = 128
    messages = @(@{ role = 'user'; content = '只回复：Anthropic proxy OK' })
    stream = [bool]$Stream
}
$body = $bodyObject | ConvertTo-Json -Depth 20 -Compress

Write-Host '=== Count Tokens ==='
$countBody = @{
    model = $Model
    messages = $bodyObject.messages
} | ConvertTo-Json -Depth 20 -Compress
$countParams = @{
    Method = 'Post'
    Uri = "$BaseUrl/v1/messages/count_tokens"
    Headers = $headers
    ContentType = 'application/json; charset=utf-8'
    Body = $countBody
    TimeoutSec = 30
}
$count = Invoke-RestMethod @countParams
$count | ConvertTo-Json -Depth 10

Write-Host '=== Anthropic Messages ==='
if ($Stream) {
    $temp = Join-Path $env:TEMP ("workbuddy-anthropic-test-" + [guid]::NewGuid().ToString('N') + '.json')
    try {
        [IO.File]::WriteAllText($temp, $body, [Text.UTF8Encoding]::new($false))
        & curl.exe -N -sS "$BaseUrl/v1/messages" -H "x-api-key: $ApiKey" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' --data-binary "@$temp"
        Write-Host
    } finally {
        [IO.File]::Delete($temp)
    }
} else {
    $messageParams = @{
        Method = 'Post'
        Uri = "$BaseUrl/v1/messages"
        Headers = $headers
        ContentType = 'application/json; charset=utf-8'
        Body = $body
        TimeoutSec = 360
    }
    $response = Invoke-RestMethod @messageParams
    $response | ConvertTo-Json -Depth 20
}
