@echo off
REM ===========================================================================
REM  Document Tracker - JSAP budget-approval sync
REM  Mirrors JSAP's decision onto invoices parked at the JSAP Approval desk:
REM  approved ones advance, rejected ones go back to SAP Approval carrying the
REM  approver's own reason, pending ones stay. Nothing is written to JSAP.
REM  Idempotent - safe to run on a schedule.
REM
REM  Run periodically via Task Scheduler, e.g. every 10 minutes:
REM    schtasks /create /tn "Tracker JSAP Sync" /tr "c:\Users\Mukesh\Desktop\OMS\OMS-Backend\run_jsap_sync.bat" /sc minute /mo 10
REM  Adjust PROJECT / PYTHON below if the paths change.
REM ===========================================================================
setlocal
set PROJECT=c:\Users\Mukesh\Desktop\OMS\OMS-Backend
set PYTHON=%PROJECT%\.venv\Scripts\python.exe

cd /d "%PROJECT%"

echo [%date% %time%] JSAP sync starting...
"%PYTHON%" manage.py sync_jsap
echo [%date% %time%] JSAP sync done.
