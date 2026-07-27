[CmdletBinding()]
param([string]$Python = 'python')
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
& $Python (Join-Path $Root 'workbuddy_to_api.py') --status
exit $LASTEXITCODE
