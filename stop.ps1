[CmdletBinding()]
param(
    [string]$ApiKey = $(if ($env:PROXY_API_KEY) { $env:PROXY_API_KEY } else { 'local' }),
    [string]$Python = 'python'
)
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
& $Python (Join-Path $Root 'workbuddy_to_api.py') --stop --api-key $ApiKey
exit $LASTEXITCODE
