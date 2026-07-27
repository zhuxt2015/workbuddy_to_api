[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:3000',
    [string]$ApiKey = 'local',
    [string]$Server = '',
    [switch]$Refresh,
    [switch]$Reload
)

$ErrorActionPreference = 'Stop'
$BaseUrl = $BaseUrl.TrimEnd('/')
$Headers = @{}
if ($ApiKey) { $Headers.Authorization = "Bearer $ApiKey" }

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

Write-Host '[1/4] MCP server inventory'
$servers = Invoke-RestMethod -Uri "$BaseUrl/admin/mcp/servers" -Headers $Headers
Assert-True ($servers.object -eq 'list') 'Unexpected server inventory response.'
Write-Host ("  source={0} servers={1} enabled={2}" -f $servers.summary.source, $servers.summary.server_count, $servers.summary.enabled_server_count)
foreach ($item in $servers.data) {
    Write-Host ("  - {0} [{1}] source={2} status={3}" -f $item.name, $item.transport, $item.source, $item.status)
}

if ($Reload) {
    Write-Host '[2/4] Reload MCP configuration'
    $reloaded = Invoke-RestMethod -Uri "$BaseUrl/admin/mcp/reload" -Method Post -Headers $Headers -ContentType 'application/json' -Body '{}'
    Assert-True ($reloaded.status -eq 'ok') 'MCP reload failed.'
    Write-Host ("  changed={0} restarted_gateways={1}" -f $reloaded.changed, $reloaded.restarted_gateways)
}
else {
    Write-Host '[2/4] Reload skipped (use -Reload to include it)'
}

Write-Host '[3/4] MCP tool inventory'
$query = @()
if ($Server) { $query += 'server=' + [uri]::EscapeDataString($Server) }
if ($Refresh) { $query += 'refresh=1' }
$toolUrl = "$BaseUrl/admin/mcp/tools"
if ($query.Count) { $toolUrl += '?' + ($query -join '&') }
$tools = Invoke-RestMethod -Uri $toolUrl -Headers $Headers
Assert-True ($tools.object -eq 'list') 'Unexpected tool inventory response.'
Write-Host ("  tools={0}" -f $tools.tool_count)
foreach ($result in $tools.servers) {
    Write-Host ("  - {0}: status={1} tools={2} latency={3}ms" -f $result.name, $result.status, $result.tool_count, $result.latency_ms)
    if ($result.error) { Write-Warning $result.error }
}

Write-Host '[4/4] Deterministic fixture call when available'
$fixture = @($tools.data | Where-Object { $_.name -eq 'proxy_echo' -and $_.server -eq 'proxy-fixture' } | Select-Object -First 1)
if ($fixture.Count) {
    $body = @{
        server = 'proxy-fixture'
        tool = 'proxy_echo'
        arguments = @{ text = 'MCP_ADMIN_TEST' }
    } | ConvertTo-Json -Depth 8
    $tested = Invoke-RestMethod -Uri "$BaseUrl/admin/mcp/test" -Method Post -Headers $Headers -ContentType 'application/json' -Body $body
    $text = [string]$tested.call.content[0].text
    Assert-True ($tested.status -eq 'ok') 'Fixture test status is not ok.'
    Assert-True ($text -eq 'MCP_ECHO_OK:MCP_ADMIN_TEST') "Unexpected fixture result: $text"
    Write-Host "  $text"
}
else {
    Write-Host '  proxy-fixture is not active; inventory test completed.'
}

Write-Host 'MCP admin tests passed.'
