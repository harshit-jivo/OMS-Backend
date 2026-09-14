@echo off
REM ===========================================================================
REM  Document Tracker - JSAP budget-approval sync
REM  Mirrors JSAP's decision onto invoices parked at the JSAP Approval desk:
REM  approved ones advance, rejected ones go back to SAP Approval carrying the
REM  approver's own reason, pending ones stay. Nothing is written to JSAP.
REM  Idempotent - safe to run on a schedule.
REM
REM  Run periodically via Task Scheduler, e.g. every 10 minutes:
REM    schtasks /create /tn "Tracker JSAP Sync" /tr "C:\LiveProjects\OMS\Backend\run_jsap_sync.bat" /sc minute /mo 10
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

echo [%date% %time%] JSAP sync starting...
"%PYTHON%" manage.py sync_jsap
echo [%date% %time%] JSAP sync done.
