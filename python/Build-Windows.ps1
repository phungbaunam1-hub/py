$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    python -m pip install -r python/requirements-dev.txt -r python/requirements-web.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    New-Item -ItemType Directory -Force artifacts | Out-Null
    python scripts/package-python-single.py --output artifacts/AI_MT_Trading.py
    if ($LASTEXITCODE -ne 0) { throw 'Single-file Python packaging failed.' }
    python -m PyInstaller --noconfirm --clean --windowed --onefile --name AI_MT_Trading `
        --paths python --hidden-import MetaTrader5 --collect-all MetaTrader5 `
        --hidden-import run_aimt --collect-submodules original `
        --collect-submodules aimt_market --collect-submodules aimt_orderflow `
        --hidden-import PyQt6.QtWebEngineWidgets --hidden-import PyQt6.QtWebEngineCore `
        --distpath python/dist --workpath python/build --specpath python/build artifacts/AI_MT_Trading.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows packaging failed.' }
    Copy-Item python/dist/AI_MT_Trading.exe artifacts/AI_MT_Trading.exe -Force
    Compress-Archive -Path python/dist/AI_MT_Trading.exe -DestinationPath artifacts/AI-MT-Trading-Python-Windows-x64.zip -Force
    python scripts/package-python-source.py --output artifacts/AI-MT-Trading-Python-source.zip
    if ($LASTEXITCODE -ne 0) { throw 'Source packaging failed.' }
    Get-FileHash artifacts/AI_MT_Trading.exe,artifacts/AI_MT_Trading.py -Algorithm SHA256 |
        ForEach-Object { $_.Hash.ToLowerInvariant() + '  ' + (Split-Path -Leaf $_.Path) } |
        Set-Content artifacts/AI-MT-Trading-single-file.sha256.txt -Encoding ascii
} finally { Pop-Location }
