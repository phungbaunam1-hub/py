$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    python -m pip install -r python/requirements-dev.txt -r python/requirements-web.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    python -m PyInstaller --noconfirm --clean --windowed --onedir --name AI_MT_Trading `
        --paths python --hidden-import MetaTrader5 --collect-all MetaTrader5 `
        --hidden-import PyQt6.QtWebEngineWidgets --hidden-import PyQt6.QtWebEngineCore `
        --distpath python/dist --workpath python/build --specpath python/build python/run_aimt.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows packaging failed.' }
    Copy-Item python/README.md python/dist/AI_MT_Trading/README.md -Force
    New-Item -ItemType Directory -Force artifacts | Out-Null
    Compress-Archive -Path python/dist/AI_MT_Trading -DestinationPath artifacts/AI-MT-Trading-Python-Windows-x64.zip -Force
    python scripts/package-python-source.py --output artifacts/AI-MT-Trading-Python-source.zip
    if ($LASTEXITCODE -ne 0) { throw 'Source packaging failed.' }
} finally { Pop-Location }
