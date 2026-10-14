param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Local', 'Vps')]
    [string]$Mode,
    [string]$VpsUrl = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    throw '.env was not found. Create and configure it from .env.example first.'
}

if ($Mode -eq 'Vps') {
    $uri = $null
    if (-not [Uri]::TryCreate($VpsUrl, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https' -or -not $uri.Host) {
        throw 'The VPS URL must be a complete HTTPS URL, for example https://chat.example.com.'
    }
    $baseUrl = $VpsUrl.TrimEnd('/')
    $allowRemote = 'true'
} else {
    $baseUrl = 'http://127.0.0.1:8080'
    $allowRemote = 'false'
}

$lines = [System.Collections.Generic.List[string]]::new()
[System.IO.File]::ReadAllLines($envPath, [System.Text.UTF8Encoding]::new($false)) | ForEach-Object { [void]$lines.Add($_) }
function Set-DotEnvValue([string]$Name, [string]$Value) {
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match "^$([Regex]::Escape($Name))=") {
            $lines[$index] = "$Name=$Value"
            return
        }
    }
    [void]$lines.Add("$Name=$Value")
}
Set-DotEnvValue 'WECHAT_BRIDGE_BASE_URL' $baseUrl
Set-DotEnvValue 'WECHAT_BRIDGE_ALLOW_REMOTE' $allowRemote
[System.IO.File]::WriteAllLines($envPath, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Host "Host bridge target switched to: $baseUrl"
Write-Host 'No service was started or stopped. Verify the single data source before starting the core and bridge.'
