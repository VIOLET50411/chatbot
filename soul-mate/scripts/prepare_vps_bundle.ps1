param(
    [string]$OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$bundleRoot = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $projectRoot 'vps-bundles' }
$stage = Join-Path $bundleRoot "soul-mate-vps-$timestamp"
$zipPath = "$stage.zip"
if ($stage.StartsWith((Join-Path $projectRoot 'data'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output directory cannot be inside data.'
}
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$resolvedStage = [IO.Path]::GetFullPath($stage).TrimEnd([IO.Path]::DirectorySeparatorChar)
function Assert-InStage([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($resolvedStage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the migration staging directory: $resolved"
    }
}

$sourceDirectories = @('api','bridge','core','host_bridge','memory','persona','scheduler','scripts','web','deploy')
foreach ($name in $sourceDirectories) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination (Join-Path $stage $name) -Recurse
}
$sourceFiles = @('Dockerfile','docker-compose.yml','requirements.txt','requirements-host.txt','runtime-defaults.env','.dockerignore','.env.example')
foreach ($name in $sourceFiles) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination (Join-Path $stage $name)
}

$temporaryDirectories = Get-ChildItem -LiteralPath $stage -Recurse -Directory | Where-Object { $_.Name -in @('__pycache__','node_modules','.pytest_cache','static') } | Sort-Object FullName -Descending
foreach ($directory in $temporaryDirectories) {
    Assert-InStage $directory.FullName
    Remove-Item -LiteralPath $directory.FullName -Recurse -Force
}
$sensitiveOrGeneratedFiles = Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object {
    $_.Extension -in @('.pyc','.log','.db','.sqlite','.sqlite3') -or
    $_.Name -like '*.tsbuildinfo' -or
    ($_.Name -like '.env*' -and $_.Name -notlike '*.example')
}
foreach ($file in $sensitiveOrGeneratedFiles) {
    Assert-InStage $file.FullName
    Remove-Item -LiteralPath $file.FullName -Force
}

$dataTarget = Join-Path $stage 'data'
& python (Join-Path $projectRoot 'scripts\export_vps_data.py') --source (Join-Path $projectRoot 'data') --target $dataTarget
if ($LASTEXITCODE -ne 0) { throw 'Private data export failed.' }
Compress-Archive -LiteralPath $stage -DestinationPath $zipPath -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $zipPath)) { throw 'Migration archive creation failed.' }
if ([IO.Path]::GetFullPath($stage) -ne $resolvedStage) { throw 'Staging directory validation failed.' }
Remove-Item -LiteralPath $resolvedStage -Recurse -Force
Write-Host "VPS migration bundle created: $zipPath"
Write-Warning 'The archive contains chats, memories, and materials but not .env. Transfer and store it securely.'
