@echo off
REM ===========================================================================
REM  OMS Auto-IRN polling sweep
REM  Generates IRNs for recent SAP invoices that don't have one yet (any source:
REM  OMS-created, SAP-created, or approval-flow). Idempotent — already-done
REM  invoices are skipped. Every attempt is logged to einvoice_irn_generation_log.
REM
REM  Run periodically via Task Scheduler (see the schtasks command in the docs).
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

echo [%date% %time%] Auto-IRN sweep starting...
"%PYTHON%" manage.py auto_generate_irns --company-db JIVO_OIL_HANADB --limit 30
"%PYTHON%" manage.py auto_generate_irns --company-db JIVO_BEVERAGES_HANADB --limit 30
echo [%date% %time%] Auto-IRN sweep done.
endlocal
