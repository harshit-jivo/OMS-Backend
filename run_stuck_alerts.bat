@echo off
REM ===========================================================================
REM  Document Tracker — stuck-invoice alert sweep
REM  Raises alerts for invoices sitting past their stage threshold and resolves
REM  alerts once an invoice moves on. Idempotent — safe to run on a schedule.
REM
REM  Run periodically via Task Scheduler, e.g. every 30 minutes:
REM    schtasks /create /tn "Tracker Stuck Alerts" /tr "c:\Users\Mukesh\Desktop\OMS\OMS-Backend\run_stuck_alerts.bat" /sc minute /mo 30
REM  Adjust PROJECT / PYTHON below if the paths change.
REM ===========================================================================
setlocal
set PROJECT=c:\Users\Mukesh\Desktop\OMS\OMS-Backend
set PYTHON=%PROJECT%\.venv\Scripts\python.exe

cd /d "%PROJECT%"

echo [%date% %time%] Stuck-alert sweep starting...
"%PYTHON%" manage.py scan_stuck_alerts
echo [%date% %time%] Stuck-alert sweep done.
