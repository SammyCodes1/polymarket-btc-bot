@echo off
REM Starts the local dashboard for the Polymarket BTC bot and opens it in your browser.
REM Usage: double-click, or run from a terminal in this folder.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [!] .venv not found. Create it first:
  echo     py -3.12 -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"

python -c "import fastapi, uvicorn, psutil" 2>nul
if errorlevel 1 (
  echo Installing dashboard dependencies...
  python -m pip install -q "fastapi>=0.110" "uvicorn>=0.29" "psutil>=5.9"
)

echo.
echo  Dashboard: http://127.0.0.1:8787/
echo  Close this window to stop the dashboard. The bot keeps running on its own.
echo.
python dashboard\server.py --open %*
if errorlevel 1 pause
endlocal
