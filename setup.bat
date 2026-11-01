@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist "config.yaml" copy /y "config.example.yaml" "config.yaml" >nul
python -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'"
if errorlevel 1 goto fail
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt --timeout 20 --retries 1 --progress-bar off
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --retries 1
if errorlevel 1 goto fail
echo Ready. Run start.bat to open the local dashboard.
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.12+ or check the error above.
pause
exit /b 1
