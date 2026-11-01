@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
if "%~1"=="" (
  ".venv\Scripts\python.exe" main.py web --open
) else (
  ".venv\Scripts\python.exe" main.py %*
)
if errorlevel 1 pause
