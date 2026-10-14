param([switch]$Install)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv-host\Scripts\python.exe'
Set-Location -LiteralPath $projectRoot
if ($Install) {
    if (-not (Test-Path -LiteralPath $venvPython)) { py -m venv (Join-Path $projectRoot '.venv-host') }
    & $venvPython -m pip install -r (Join-Path $projectRoot 'requirements-host.txt')
}
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run this script with -Install to install host dependencies first.' }
& $venvPython -c "from dotenv import load_dotenv; import os; load_dotenv('.env'); raise SystemExit(0 if len(os.getenv('SOCIAL_BROWSER_PROXY_TOKEN','').strip()) >= 24 else 2)"
if ($LASTEXITCODE -ne 0) { throw 'Set SOCIAL_BROWSER_PROXY_TOKEN in .env to at least 24 characters first.' }
& $venvPython -m uvicorn host_bridge.social_browser_proxy:app --host 0.0.0.0 --port 8280
exit $LASTEXITCODE
