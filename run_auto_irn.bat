@echo off
REM ===========================================================================
REM  OMS Auto-IRN polling sweep
REM  Generates IRNs for recent SAP invoices that don't have one yet (any source:
REM  OMS-created, SAP-created, or approval-flow). Idempotent — already-done
REM  invoices are skipped. Every attempt is logged to einvoice_irn_generation_log.
REM
REM  Run periodically via Task Scheduler (see the schtasks command in the docs).
REM  Adjust PROJECT / PYTHON below if the paths change.
REM ===========================================================================
setlocal
set PROJECT=c:\Users\Mukesh\Desktop\OMS\OMS-Backend
set PYTHON=%PROJECT%\.venv\Scripts\python.exe

cd /d "%PROJECT%"

echo [%date% %time%] Auto-IRN sweep starting...
"%PYTHON%" manage.py auto_generate_irns --company-db JIVO_OIL_HANADB --limit 30
"%PYTHON%" manage.py auto_generate_irns --company-db JIVO_BEVERAGES_HANADB --limit 30
echo [%date% %time%] Auto-IRN sweep done.
endlocal
