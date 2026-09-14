@echo off
REM ===========================================================================
REM  Document Tracker - SAP-saved sweep
REM
REM  Asks SAP whether each in-progress tracker invoice already exists as a
REM  POSTED A/P invoice or credit memo (OPCH / ORPC, not cancelled, matched on
REM  vendor code + vendor invoice number). Any that do are walked to the
REM  Payment stage, with every desk in between recorded as a skipped visit
REM  carrying the reason and the SAP document number.
REM
REM  Idempotent - an invoice already at Payment is never a candidate, so a
REM  repeat run changes nothing.
REM
REM  Scheduled DAILY AT MIDNIGHT. Create the task (run once, as Administrator):
REM
REM    schtasks /create /tn "Tracker SAP Saved Sync" ^
REM             /tr "C:\LiveProjects\OMS\Backend\run_sap_saved_sync.bat" ^
REM             /sc daily /st 00:00 /ru SYSTEM /rl HIGHEST /f
REM
REM  Adjust PROJECT / PYTHON below if the paths change.
REM ===========================================================================
setlocal
set PROJECT=C:\LiveProjects\OMS\Backend
set PYTHON=%PROJECT%\.venv\Scripts\python.exe
set LOGDIR=%PROJECT%\logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%PROJECT%"

echo [%date% %time%] SAP-saved sweep starting... >> "%LOGDIR%\sap_saved_sync.log"
"%PYTHON%" manage.py sync_sap_saved --verbose >> "%LOGDIR%\sap_saved_sync.log" 2>&1
echo [%date% %time%] SAP-saved sweep done (exit %ERRORLEVEL%). >> "%LOGDIR%\sap_saved_sync.log"
