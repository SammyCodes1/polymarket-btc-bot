@echo off
REM One-click launcher for the Polymarket BTC bot dashboard (Windows).
REM First run: creates .venv and installs requirements. Then starts the dashboard and opens your browser.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating the Python environment in .venv ...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [!] Could not create .venv. Install Python 3.11 or newer from https://www.python.org/downloads/
    echo     ^(tick "Add python.exe to PATH" during install^), then run this file again.
    pause
    exit /b 1
  )
  echo Installing requirements ^(this can take a minute^) ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [!] pip install failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, psutil, requests, dotenv" 2>nul
if errorlevel 1 (
  echo Installing missing requirements ...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo.
echo  Dashboard: http://127.0.0.1:8787/
echo  Close this window to stop the dashboard. A running bot keeps running on its own.
echo.
".venv\Scripts\python.exe" dashboard\server.py --open %*
if errorlevel 1 pause
endlocal
