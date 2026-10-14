param(
    [switch]$Install,
    [switch]$CheckOnly,
    [switch]$ValidationMode,
    [string]$AccountId = ''
)

$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv-host\Scripts\python.exe'
$wxautoxExe = Join-Path $projectRoot '.venv-host\Scripts\wxautox4.exe'
Set-Location -LiteralPath $projectRoot

if ($Install) {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        py -m venv (Join-Path $projectRoot '.venv-host')
    }
    & $venvPython -m pip install -r (Join-Path $projectRoot 'requirements-host.txt')
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'The host bridge is not installed. Run .\scripts\start_wxautox_bridge.ps1 -Install first.'
}

$bridgeBaseUrl = (& $venvPython -c "from dotenv import load_dotenv; import os; load_dotenv('.env'); print(os.getenv('WECHAT_BRIDGE_BASE_URL','http://127.0.0.1:8080').rstrip('/'))").Trim()
if (-not $bridgeBaseUrl) { $bridgeBaseUrl = 'http://127.0.0.1:8080' }
try {
    $health = Invoke-RestMethod -Uri "$bridgeBaseUrl/healthz" -TimeoutSec 8
    if ($health.status -ne 'ok') { throw 'The core health response is invalid.' }
} catch {
    throw "The core is not ready at $bridgeBaseUrl. Start local Docker or check VPS HTTPS."
}

& $venvPython -c 'import wxautox4' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'wxautox4 is not installed. Run this script again with -Install.'
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$authLines = @(& $wxautoxExe auth check --json 2>&1)
$authExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
$authText = (($authLines | ForEach-Object { $_.ToString() }) | Out-String).Trim()
$authJsonStart = $authText.IndexOf('{')
$authJsonEnd = $authText.LastIndexOf('}')
$authJson = ''
if ($authJsonStart -ge 0 -and $authJsonEnd -ge $authJsonStart) {
    $authJson = $authText.Substring($authJsonStart, $authJsonEnd - $authJsonStart + 1)
}
try {
    $authState = $authJson | ConvertFrom-Json -ErrorAction Stop
} catch {
    throw "Unable to parse Plus authorization (exit $authExitCode). Run .\.venv-host\Scripts\wxautox4.exe auth check --json in this same PowerShell window."
}
if (-not $authState.active) {
    throw 'Plus is inactive in this Windows account or privilege context. Activate it in this same PowerShell window, then rerun the check.'
}
$env:WECHAT_PLUS_ACTIVE = 'true'

$agentArgs = @('-m', 'scripts.wxautox_bridge_agent')
if ($CheckOnly) { $agentArgs += '--check' }
if ($ValidationMode) { $agentArgs += '--validation-mode' }
if ($AccountId) { $agentArgs += @('--account-id', $AccountId) }
& $venvPython @agentArgs
exit $LASTEXITCODE
