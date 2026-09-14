@echo off
REM ===========================================================================
REM  Document Tracker — stuck-invoice alert sweep
REM  Raises alerts for invoices sitting past their stage threshold and resolves
REM  alerts once an invoice moves on. Idempotent — safe to run on a schedule.
REM
REM  Run periodically via Task Scheduler, e.g. every 30 minutes:
REM    schtasks /create /tn "Tracker Stuck Alerts" /tr "C:\LiveProjects\OMS\Backend\run_stuck_alerts.bat" /sc minute /mo 30
REM  PROJECT is THIS script's own directory (%~dp0), not a hardcoded path, so
REM  the same file is correct on the server and in a dev checkout.
REM ===========================================================================
setlocal
set PROJECT=%~dp0
if "%PROJECT:~-1%"=="\" set PROJECT=%PROJECT:~0,-1%
set PYTHON=%PROJECT%\.venv\Scripts\python.exe

REM Fail loudly rather than running against a directory that is not there:
REM a silent `cd` failure is what let a mis-pathed task look like it was
REM running for weeks while doing nothing.
if not exist "%PYTHON%" (
  echo [%date% %time%] ERROR: no Python at "%PYTHON%" - is the venv built?
  exit /b 1
)

cd /d "%PROJECT%"

echo [%date% %time%] Stuck-alert sweep starting...
"%PYTHON%" manage.py scan_stuck_alerts
echo [%date% %time%] Stuck-alert sweep done.
