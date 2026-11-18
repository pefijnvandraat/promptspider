param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [string]$OutputDirectory = 'dist',
    [string]$Compiler = 'ISCC.exe'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    $output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
    if (-not (Get-Command $Compiler -ErrorAction SilentlyContinue)) {
        throw 'Install Inno Setup 6 and pass the full path to its ISCC.exe with -Compiler.'
    }

    $build = Join-Path $output 'build'
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    $venv = Join-Path $build 'venv'
    & py -3.12-64 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 (64-bit) is required on the build machine.' }
    $python = Join-Path $venv 'Scripts\python.exe'
    & $python -m pip --disable-pip-version-check install -q -r windows\requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Windows build dependencies.' }

    & $python -m PyInstaller --noconfirm --clean --onedir --windowed --name PromptSpider `
        --log-level WARN --specpath $build --workpath (Join-Path $build 'work') `
        --distpath $build --add-data "$(Join-Path $root 'static\index.html'):static" `
        --hidden-import uvicorn.logging --hidden-import uvicorn.loops.asyncio `
        --hidden-import uvicorn.protocols.http.h11_impl desktop.py
    if ($LASTEXITCODE -ne 0) { throw 'Could not package Prompt Spider.' }

    $bundle = Join-Path $build 'PromptSpider'
    $check = Start-Process (Join-Path $bundle 'PromptSpider.exe') -ArgumentList '--check' -Wait -PassThru
    if ($check.ExitCode -ne 0) { throw 'The packaged app failed its startup check.' }

    & $Compiler /Qp "/DAppVersion=$Version" "/DBundleDir=$bundle" "/O$output" windows\PromptSpider.iss
    if ($LASTEXITCODE -ne 0) { throw 'Could not compile the Windows installer.' }
    Write-Output (Join-Path $output 'PromptSpider-Setup.exe')
} finally {
    Pop-Location
}
