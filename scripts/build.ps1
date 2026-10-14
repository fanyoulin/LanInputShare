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
    & $python -m PyInstaller --noconfirm --clean packaging\lan-input-share.spec
    Assert-NativeSuccess 'Build executable'
}
finally {
    $env:Path = $savedBuildPath
}

$exePath = Join-Path $distPath 'LanInputShare\LanInputShare.exe'
& $exePath --gui-smoke-test
Assert-NativeSuccess 'Load packaged Qt interface'
& $exePath --diagnose
Assert-NativeSuccess 'Run packaged diagnostic'

$manifestPath = Join-Path $distPath 'LanInputShare.manifest.txt'
Get-ChildItem -LiteralPath (Join-Path $distPath 'LanInputShare') -Recurse -File |
    ForEach-Object { $_.FullName.Substring($distPath.Length + 1) } |
    Sort-Object |
    Set-Content -LiteralPath $manifestPath -Encoding utf8

$hash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
"$hash  LanInputShare/LanInputShare.exe" |
    Set-Content -LiteralPath (Join-Path $distPath 'LanInputShare.sha256') -Encoding ascii

Write-Host "Build complete: $exePath"
Write-Host "SHA256: $hash"
