[CmdletBinding()]
param(
    [int]$Port = 3000,
    [string]$HostAddress = '127.0.0.1',
    [string]$ApiKey = 'local',
    [string]$Model = 'auto',
    [string]$WorkingDirectory = '',
    [switch]$DisableTools,
    [string]$Tools = 'default',
    [int]$MaxTurns = 8,
    [string]$PermissionMode = 'bypassPermissions',
    [string]$McpConfig = '',
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $WorkingDirectory) { $WorkingDirectory = Split-Path -Parent $Root }
$Arguments = @(
    (Join-Path $Root 'workbuddy_to_api.py'),
    '--host', $HostAddress,
    '--port', [string]$Port,
    '--api-key', $ApiKey,
    '--model', $Model,
    '--cwd', (Resolve-Path -LiteralPath $WorkingDirectory).Path,
    '--tools', $Tools,
    '--max-turns', [string]$MaxTurns,
    '--permission-mode', $PermissionMode
)
if ($DisableTools) { $Arguments += '--disable-tools' }
if ($McpConfig) { $Arguments += @('--mcp-config', $McpConfig) }
& $Python @Arguments
exit $LASTEXITCODE
