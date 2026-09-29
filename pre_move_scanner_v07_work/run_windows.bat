@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Pre-Move Scanner

rem  Usage:
rem    run_windows.bat                 start the live scanner  (http://127.0.0.1:8000)
rem    run_windows.bat selftest        live self-test: universe, venues, websockets  (writes data\selftest_report.md)
rem        options, e.g.:  selftest --repeat 3          three streaming rounds
rem                        selftest --assets QNT,XDC,LINK --exchanges kucoin --repeat 3
rem    run_windows.bat walletcheck     live wallet-provider check: CoinGecko discovery + every chain provider
rem        options, e.g.:  walletcheck --only discovery,bitcoin,evm    (writes data\wallet_check_report.md)
rem    run_windows.bat stress          live start/stop/reconnect stress test of one exchange (default KuCoin)
rem        options, e.g.:  stress --exchange kucoin --assets QNT,XDC,LINK --cycles 20
rem    run_windows.bat sim             offline demo with synthetic markets
rem    run_windows.bat import "C:\path\to\v0.6\scanner.db"   read-only import of v0.6 history
rem    run_windows.bat test            run the automated test-suite
rem    run_windows.bat release         build a clean release ZIP in dist\ (never contains data, config or keys)
rem  Everything after the action is passed on unchanged (commas included).
rem  Set PMS_NO_PAUSE=1 to skip the "press any key" prompts (automation).
rem  The version shown comes from server\__init__.py (the one canonical version).

set "RC=0"
rem  (single lines, not a parenthesised block: a ")" in a path must not end the block)
set "ARGS=%*"
if defined ARGS call set "ARGS=%%ARGS:*%1=%%"

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

rem  config.json only if missing (never overwritten), data\ if missing, database state (never replaced)
"%PY%" tools\bootstrap.py
if errorlevel 1 goto :bootstraperror
set "PMS_VERSION="
"%PY%" tools\bootstrap.py --version > "%TEMP%\pms_version.txt" 2>nul
set /p PMS_VERSION=<"%TEMP%\pms_version.txt"
del "%TEMP%\pms_version.txt" >nul 2>&1
title Pre-Move Scanner v%PMS_VERSION%

if /I "%~1"=="selftest" goto :selftest
if /I "%~1"=="walletcheck" goto :walletcheck
if /I "%~1"=="stress" goto :stress
if /I "%~1"=="sim" goto :sim
if /I "%~1"=="import" goto :import
if /I "%~1"=="test" goto :test
if /I "%~1"=="release" goto :release

echo.
echo Starting Pre-Move Scanner v%PMS_VERSION% - live mode
echo Open http://127.0.0.1:8000   - phones on the same Wi-Fi: http://YOUR-PC-IP:8000
echo The first ~30 minutes are a warm-up while baselines are built.
echo.
"%PY%" -m uvicorn server.app:app --host 0.0.0.0 --port 8000
goto :end

:selftest
"%PY%" tools\selftest.py %ARGS%
set "RC=%ERRORLEVEL%"
echo.
echo Report: data\selftest_report.md
if not defined PMS_NO_PAUSE pause
goto :end

:walletcheck
"%PY%" tools\wallet_check.py %ARGS%
set "RC=%ERRORLEVEL%"
echo.
echo Report: data\wallet_check_report.md
if not defined PMS_NO_PAUSE pause
goto :end

:stress
"%PY%" tools\feed_stress.py %ARGS%
set "RC=%ERRORLEVEL%"
echo.
echo Report: data\feed_stress_^<exchange^>.md
if not defined PMS_NO_PAUSE pause
goto :end

:sim
set "PMS_MODE=sim"
echo Starting OFFLINE DEMO v%PMS_VERSION% with synthetic markets on http://127.0.0.1:8001 ...
"%PY%" -m uvicorn server.app:app --host 127.0.0.1 --port 8001
goto :end

:import
if "%~2"=="" (
  echo Usage: run_windows.bat import "C:\path\to\pre_move_scanner\scanner.db"
  goto :end
)
"%PY%" tools\import_v06.py "%~2"
set "RC=%ERRORLEVEL%"
if not defined PMS_NO_PAUSE pause
goto :end

:test
"%PY%" -m pip install --disable-pip-version-check -q -r requirements-dev.txt
"%PY%" -m unittest discover -s tests -t . -v
set "RC=%ERRORLEVEL%"
if not defined PMS_NO_PAUSE pause
goto :end

:release
"%PY%" tools\make_release.py %ARGS%
set "RC=%ERRORLEVEL%"
if not defined PMS_NO_PAUSE pause
goto :end

:nopython
echo.
echo Could not create a Python virtual environment.
echo Install Python 3.11 or newer from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
set "RC=1"
if not defined PMS_NO_PAUSE pause
goto :end

:piperror
echo.
echo Installing dependencies failed. Check your internet connection and the messages above.
echo Tip: delete the .venv folder and run this file again (your data and config.json are not touched).
set "RC=1"
if not defined PMS_NO_PAUSE pause
goto :end

:bootstraperror
echo.
echo The first-run setup (tools\bootstrap.py) failed - see the messages above.
set "RC=1"
if not defined PMS_NO_PAUSE pause

:end
endlocal & exit /b %RC%
