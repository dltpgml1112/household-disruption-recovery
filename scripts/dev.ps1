# Start the MCP server (8765), the simulator API (8766) and the web simulator (3000).
# Close the window or press Ctrl+C to stop; child processes are stopped on exit.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $env:HDR_DB_PATH) { $env:HDR_DB_PATH = "var/hdr.sqlite3" }
if (-not $env:HDR_APPROVAL_SECRET) { $env:HDR_APPROVAL_SECRET = (uv run python -c "import secrets; print(secrets.token_hex(32))") }
if (-not $env:HDR_ADMIN_TOKEN) { $env:HDR_ADMIN_TOKEN = (uv run python -c "import secrets; print(secrets.token_hex(16))") }
if (-not $env:HDR_MCP_URL) { $env:HDR_MCP_URL = "http://127.0.0.1:8765/mcp" }
if (-not $env:NEXT_PUBLIC_HDR_API) { $env:NEXT_PUBLIC_HDR_API = "http://127.0.0.1:8766" }

if (-not (Test-Path "apps/simulator/node_modules")) {
    Push-Location apps/simulator; npm ci --no-audit --no-fund; Pop-Location
}

$procs = @()
try {
    $procs += Start-Process -PassThru -NoNewWindow uv -ArgumentList "run", "python", "-m", "hdr.mcp_server"
    $procs += Start-Process -PassThru -NoNewWindow uv -ArgumentList "run", "python", "-m", "hdr.orchestrator"
    $procs += Start-Process -PassThru -NoNewWindow -WorkingDirectory "apps/simulator" npm.cmd -ArgumentList "run", "dev"
    Write-Host "MCP server   http://127.0.0.1:8765/mcp"
    Write-Host "Simulator    http://127.0.0.1:3000"
    Wait-Process -Id ($procs | ForEach-Object { $_.Id })
}
finally {
    $procs | ForEach-Object { if (-not $_.HasExited) { Stop-Process -Id $_.Id -Force } }
}
