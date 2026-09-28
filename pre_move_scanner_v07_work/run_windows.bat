@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Pre-Move Scanner v0.7

rem  Usage:
rem    run_windows.bat                 start the live scanner  (http://127.0.0.1:8000)
rem    run_windows.bat selftest        live self-test: universe, venues, websockets  (writes data\selftest_report.md)
rem    run_windows.bat sim             offline demo with synthetic markets
rem    run_windows.bat import "C:\path\to\v0.6\scanner.db"   read-only import of v0.6 history
rem    run_windows.bat test            run the automated test-suite

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [setup] Creating virtual environment .venv ...
  py -3 -m venv .venv >nul 2>&1
  if not exist "%PY%" python -m venv .venv
  if not exist "%PY%" goto :nopython
)

echo [setup] Installing / updating dependencies ...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :piperror

if not exist "config.json" (
  echo [setup] Creating config.json from config.example.json - an existing config.json is never overwritten
  copy /Y "config.example.json" "config.json" >nul
)
if not exist "data" mkdir "data"

if /I "%~1"=="selftest" goto :selftest
if /I "%~1"=="sim" goto :sim
if /I "%~1"=="import" goto :import
if /I "%~1"=="test" goto :test

echo.
echo Starting Pre-Move Scanner v0.7 - live mode
echo Open http://127.0.0.1:8000   - phones on the same Wi-Fi: http://YOUR-PC-IP:8000
echo The first ~30 minutes are a warm-up while baselines are built.
echo.
"%PY%" -m uvicorn server.app:app --host 0.0.0.0 --port 8000
goto :end

:selftest
"%PY%" tools\selftest.py %2 %3 %4 %5
echo.
echo Report: data\selftest_report.md
pause
goto :end

:sim
set "PMS_MODE=sim"
echo Starting OFFLINE DEMO with synthetic markets on http://127.0.0.1:8001 ...
"%PY%" -m uvicorn server.app:app --host 127.0.0.1 --port 8001
goto :end

:import
if "%~2"=="" (
  echo Usage: run_windows.bat import "C:\path\to\pre_move_scanner\scanner.db"
  goto :end
)
"%PY%" tools\import_v06.py "%~2"
pause
goto :end

:test
"%PY%" -m pip install --disable-pip-version-check -q -r requirements-dev.txt
"%PY%" -m unittest discover -s tests -t . -v
pause
goto :end

:nopython
echo.
echo Could not create a Python virtual environment.
echo Install Python 3.11 or newer from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
goto :end

:piperror
echo.
echo Installing dependencies failed. Check your internet connection and the messages above.
echo Tip: delete the .venv folder and run this file again.
pause

:end
endlocal
