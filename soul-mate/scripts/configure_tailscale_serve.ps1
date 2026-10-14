param(
    [switch]$Disable
)

$ErrorActionPreference = 'Stop'
$tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $tailscale) {
    throw 'Tailscale was not found. Install and sign in to the Windows client, then run this script from an elevated PowerShell window.'
}

if ($Disable) {
    & $tailscale.Source serve reset
    if ($LASTEXITCODE -ne 0) { throw 'Failed to disable Tailscale Serve.' }
    Write-Host 'The private Tailscale endpoint for Soul Mate is disabled.'
    exit 0
}

try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/healthz' -TimeoutSec 5
} catch {
    throw 'The local Docker core is not healthy. Run docker compose up -d --build first.'
}
if ($health.status -ne 'ok') { throw 'The local core returned an invalid health response.' }

$statusText = (& $tailscale.Source status --json 2>$null | Out-String)
if (-not $statusText.Trim()) { throw 'Unable to read Tailscale status. Make sure the Windows client is signed in.' }
$status = $statusText | ConvertFrom-Json
if ($status.BackendState -ne 'Running') { throw "Tailscale state is $($status.BackendState). Connect to the tailnet first." }

& $tailscale.Source serve --bg --yes 8080
if ($LASTEXITCODE -ne 0) { throw 'Failed to create the private Tailscale Serve HTTPS endpoint.' }

Write-Host 'Tailscale Serve is enabled and will resume after Tailscale restarts.'
Write-Host 'Find the https://...ts.net URL below, sign the iPhone into the same tailnet, and open /chat.'
& $tailscale.Source serve status
