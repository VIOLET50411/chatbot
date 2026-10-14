[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_.@-]+$')]
    [string]$SshTarget,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://[^/\s]+(?:/[^\s]*)?$')]
    [string]$VpsUrl,

    [ValidatePattern('^/[A-Za-z0-9_./-]+$')]
    [string]$RemoteRoot = '/opt/soul-mate',

    [switch]$IUnderstandOnlyOneCoreMayWrite
)

$ErrorActionPreference = 'Stop'
if (-not $IUnderstandOnlyOneCoreMayWrite) {
    throw 'Confirm that local and VPS cores cannot write at the same time, then add -IUnderstandOnlyOneCoreMayWrite.'
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue) -or -not (Get-Command scp -ErrorAction SilentlyContinue)) {
    throw 'Windows OpenSSH Client (ssh and scp) is required.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$bundleDirectory = Join-Path $projectRoot 'vps-bundles'
$envBackup = Join-Path $projectRoot ".env.before-vps-$timestamp"
$remoteUpload = "/tmp/soul-mate-$timestamp.zip"
$remotePrevious = "$RemoteRoot.previous-$timestamp"

if (-not $PSCmdlet.ShouldProcess(
    "${SshTarget}:$RemoteRoot",
    'Stop the local core, export private data, deploy the VPS core, verify health, and switch the Windows bridge URL'
)) {
    return
}

# Stop the Plus bridge manually before the offline export.
$bridgeProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'scripts[\\/.]wxautox_bridge_agent'
}
if ($bridgeProcesses) {
    throw 'The WeChat bridge is still running. Press Ctrl+C in its PowerShell window, then retry.'
}

$localStopped = $false
try {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env') -Destination $envBackup
    & docker compose -f (Join-Path $projectRoot 'docker-compose.yml') stop core
    if ($LASTEXITCODE -ne 0) { throw 'Failed to stop the local core.' }
    $localStopped = $true

    & (Join-Path $PSScriptRoot 'prepare_vps_bundle.ps1') -OutputDirectory $bundleDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the VPS migration bundle.' }
    $bundle = Get-ChildItem -LiteralPath $bundleDirectory -Filter 'soul-mate-vps-*.zip' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $bundle) { throw 'The newly created migration bundle was not found.' }

    & scp -- $bundle.FullName "$SshTarget`:$remoteUpload"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to upload the migration bundle.' }

    $remoteScript = @"
set -eu
test -f '$RemoteRoot/deploy/vps/.env.vps'
rm -rf '/tmp/soul-mate-stage-$timestamp'
mkdir -p '/tmp/soul-mate-stage-$timestamp'
unzip -q '$remoteUpload' -d '/tmp/soul-mate-stage-$timestamp'
next_root=`$(find '/tmp/soul-mate-stage-$timestamp' -mindepth 1 -maxdepth 1 -type d | head -n 1)
test -n "`$next_root"
cp '$RemoteRoot/deploy/vps/.env.vps' "`$next_root/deploy/vps/.env.vps"
cd '$RemoteRoot'
docker compose -f deploy/vps/docker-compose.vps.yml down
cd /
mv '$RemoteRoot' '$remotePrevious'
mv "`$next_root" '$RemoteRoot'
cd '$RemoteRoot'
docker compose -f deploy/vps/docker-compose.vps.yml up -d --build
rm -f '$remoteUpload'
rm -rf '/tmp/soul-mate-stage-$timestamp'
"@
    & ssh -- $SshTarget $remoteScript
    if ($LASTEXITCODE -ne 0) { throw 'The VPS deployment command failed. The previous remote directory remains available for recovery.' }

    $deadline = (Get-Date).AddMinutes(5)
    $healthy = $false
    do {
        try {
            $health = Invoke-RestMethod -Uri ($VpsUrl.TrimEnd('/') + '/healthz') -TimeoutSec 10
            $healthy = $health.status -eq 'ok'
        } catch {
            Start-Sleep -Seconds 5
        }
    } while (-not $healthy -and (Get-Date) -lt $deadline)
    if (-not $healthy) { throw 'The VPS did not become healthy within five minutes. The Windows bridge URL was not switched.' }

    & (Join-Path $PSScriptRoot 'set_core_mode.ps1') -Mode Vps -VpsUrl $VpsUrl
    if ($LASTEXITCODE -ne 0) { throw 'The VPS is healthy, but switching the Windows bridge URL failed.' }
    Write-Host "Migration completed. The previous remote release is at: $remotePrevious"
    Write-Host 'The host WeChat bridge can now be started. Keep the local core stopped to prevent dual writes.'
} catch {
    if ($localStopped) {
        Write-Warning 'Migration did not complete. Restoring the local core and bridge configuration.'
        if (Test-Path -LiteralPath $envBackup) {
            Copy-Item -LiteralPath $envBackup -Destination (Join-Path $projectRoot '.env') -Force
        }
        & docker compose -f (Join-Path $projectRoot 'docker-compose.yml') up -d core
    }
    throw
}
