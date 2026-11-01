$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath 'config.yaml')) {
    Copy-Item -LiteralPath 'config.example.yaml' -Destination 'config.yaml'
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python venv creation failed.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt --timeout 20 --retries 1 --progress-bar off
if ($LASTEXITCODE -ne 0) {
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --retries 1 --progress-bar off
}
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Ready. Edit config.yaml, then run .\start.ps1 login' -ForegroundColor Green
Write-Host 'Uses installed Edge by default. For chromium: .\.venv\Scripts\python.exe -m playwright install chromium'
