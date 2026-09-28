@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/5] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :error
)

echo [2/5] Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [3/5] Updating v0.5 config...
if exist "config.json" copy /Y "config.json" "config.backup.json" >nul
copy /Y "config.example.json" "config.json" >nul

echo [4/5] Clearing old venue discovery cache...
if exist "venue_discovery_cache.json" del /Q "venue_discovery_cache.json"

echo [5/5] Starting dynamic per-coin scanner...
echo.
echo Each coin now discovers its own highest-volume spot venues.
echo Open: http://127.0.0.1:8000
echo.
".venv\Scripts\python.exe" -m uvicorn server.app:app --host 0.0.0.0 --port 8000
goto :eof

:error
echo.
echo Something failed. Copy this window output back to ChatGPT.
pause
