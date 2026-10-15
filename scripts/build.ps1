param(
    [switch]$KeepEnvironment
)

$ErrorActionPreference = 'Stop'

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$venvPath = Join-Path $projectRoot '.build-venv'
$buildPath = Join-Path $projectRoot 'build'
$distPath = Join-Path $projectRoot 'dist'

Set-Location -LiteralPath $projectRoot

if (-not $KeepEnvironment) {
    foreach ($path in @($venvPath, $buildPath, $distPath)) {
        $resolvedParent = (Resolve-Path -LiteralPath (Split-Path -Parent $path)).Path
        if (-not $path.StartsWith($resolvedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove path outside project: $path"
        }
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
        }
    }
}

python -m venv $venvPath
Assert-NativeSuccess 'Create build environment'
$python = Join-Path $venvPath 'Scripts\python.exe'
& $python -m pip install --upgrade pip
Assert-NativeSuccess 'Upgrade pip'
& $python -m pip install --requirement (Join-Path $projectRoot 'requirements-lock.txt')
Assert-NativeSuccess 'Install locked requirements'
& $python -m pip install --no-deps $projectRoot
Assert-NativeSuccess 'Install application'

& $python -m pytest -q
Assert-NativeSuccess 'Run tests'
& $python -m ruff check src tests
Assert-NativeSuccess 'Run Ruff'
& $python -m mypy src\lan_input_share
Assert-NativeSuccess 'Run mypy'
$savedBuildPath = $env:Path
$isolatedBuildPath = @(
    (Join-Path $venvPath 'Scripts')
    (Join-Path $env:SystemRoot 'System32')
    $env:SystemRoot
) -join ';'
try {
    $env:Path = $isolatedBuildPath
    & $python -m PyInstaller --noconfirm --clean packaging\lan-input-share-input-helper.spec
    Assert-NativeSuccess 'Build elevated input helper'
    & $python -m PyInstaller --noconfirm --clean packaging\lan-input-share.spec
    Assert-NativeSuccess 'Build GUI executable'
}
finally {
    $env:Path = $savedBuildPath
}

$releasePath = Join-Path $distPath 'LanInputShare'
$exePath = Join-Path $releasePath 'LanInputShare.exe'
$helperBuildPath = Join-Path $distPath 'LanInputShareInputHelper.exe'
$helperPath = Join-Path $releasePath 'LanInputShareInputHelper.exe'
if (-not (Test-Path -LiteralPath $helperBuildPath)) {
    throw "Missing helper build artifact: $helperBuildPath"
}
Copy-Item -LiteralPath $helperBuildPath -Destination $helperPath -Force

& $exePath --gui-smoke-test
Assert-NativeSuccess 'Load packaged Qt interface'
& $exePath --diagnose
Assert-NativeSuccess 'Run packaged diagnostic'
& $helperPath --smoke-test
Assert-NativeSuccess 'Run packaged helper smoke test'

$manifestPath = Join-Path $releasePath 'release-manifest.txt'
$hashPath = Join-Path $releasePath 'SHA256SUMS.txt'
Get-ChildItem -LiteralPath $releasePath -Recurse -File |
    Where-Object { $_.FullName -notin @($manifestPath, $hashPath) } |
    ForEach-Object { $_.FullName.Substring($releasePath.Length + 1).Replace('\', '/') } |
    Sort-Object |
    Set-Content -LiteralPath $manifestPath -Encoding utf8

$hashLines = foreach ($artifact in @('LanInputShare.exe', 'LanInputShareInputHelper.exe')) {
    $artifactPath = Join-Path $releasePath $artifact
    $hash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash
    "$hash  $artifact"
}
$hashLines | Set-Content -LiteralPath $hashPath -Encoding ascii

$manifestText = Get-Content -LiteralPath $manifestPath -Raw
if ($manifestText -match '(?m)^[A-Za-z]:\\' -or $manifestText -match '(?m)^\\\\') {
    throw 'Release manifest contains an absolute path'
}
foreach ($required in @('LanInputShare.exe', 'LanInputShareInputHelper.exe')) {
    if ($manifestText -notmatch [regex]::Escape($required)) {
        throw "Release manifest is missing $required"
    }
}
if ((Get-Content -LiteralPath $hashPath).Count -ne 2) {
    throw 'SHA256SUMS.txt must contain exactly two executable hashes'
}

Write-Host "Build complete: $exePath"
Write-Host "Helper: $helperPath"
Write-Host "Hashes: $hashPath"
