param(
    [string]$DuWechatNickname = '',
    [string]$JiWechatNickname = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) { throw '.env was not found. Create it from .env.example first.' }

if (-not $DuWechatNickname.Trim()) {
    $DuWechatNickname = Read-Host 'Enter the account nickname shown by the Du Jiaying WeChat window'
}
if (-not $JiWechatNickname.Trim()) {
    $JiWechatNickname = Read-Host 'Enter the account nickname shown by the Ji Yu WeChat window'
}
$DuWechatNickname = $DuWechatNickname.Trim()
$JiWechatNickname = $JiWechatNickname.Trim()
if (-not $DuWechatNickname -or -not $JiWechatNickname) { throw 'Both WeChat account nicknames are required.' }
if ($DuWechatNickname -eq $JiWechatNickname) { throw 'The two WeChat account nicknames must be different.' }

$accounts = @(
    [ordered]@{ account_id = 'du-jiaying'; nickname = $DuWechatNickname },
    [ordered]@{ account_id = 'ji-yu'; nickname = $JiWechatNickname }
) | ConvertTo-Json -Compress
$lines = [System.Collections.Generic.List[string]]::new()
[System.IO.File]::ReadAllLines($envPath, [System.Text.UTF8Encoding]::new($false)) | ForEach-Object { [void]$lines.Add($_) }
$updated = $false
for ($index = 0; $index -lt $lines.Count; $index++) {
    if ($lines[$index] -match '^WECHAT_ACCOUNTS_JSON=') {
        $lines[$index] = "WECHAT_ACCOUNTS_JSON=$accounts"
        $updated = $true
        break
    }
}
if (-not $updated) { [void]$lines.Add("WECHAT_ACCOUNTS_JSON=$accounts") }
[System.IO.File]::WriteAllLines($envPath, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Host 'Both WeChat windows are configured. Confirm contact names in the owner console after the bridge starts.'
