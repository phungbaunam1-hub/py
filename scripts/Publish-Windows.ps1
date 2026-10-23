param([string]$OutputDirectory = "artifacts/windows-x64")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    dotnet restore FlowDesk.sln --locked-mode
    if ($LASTEXITCODE -ne 0) { throw 'Restore failed.' }
    dotnet test tests/FlowDesk.Core.Tests -c Release --no-restore --logger trx
    if ($LASTEXITCODE -ne 0) { throw 'Core tests failed.' }
    dotnet test tests/FlowDesk.Infrastructure.Tests -c Release --no-restore --logger trx
    if ($LASTEXITCODE -ne 0) { throw 'Feed tests failed.' }
    if ($IsWindows -or $env:OS -eq 'Windows_NT') {
        dotnet test tests/FlowDesk.Desktop.Tests -c Release --no-restore --logger trx
        if ($LASTEXITCODE -ne 0) { throw 'Windows UI smoke tests failed.' }
    }
    dotnet publish src/FlowDesk.Desktop/FlowDesk.Desktop.csproj -c Release -r win-x64 `
        --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true `
        -p:RestoreLockedMode=true -p:NuGetLockFilePath=packages.win-x64.lock.json -o $OutputDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Publish failed.' }
    Copy-Item README.md $OutputDirectory
    $docsDirectory = Join-Path $OutputDirectory 'docs'
    New-Item -ItemType Directory -Force -Path $docsDirectory | Out-Null
    Copy-Item docs/*.md $docsDirectory -Force
    Write-Host "Windows x64 demo: $OutputDirectory/FlowDesk.Desktop.exe"
} finally {
    Pop-Location
}
