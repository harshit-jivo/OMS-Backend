@echo off
REM ===========================================================================
REM  Document Tracker - stuck-invoice EMAIL sweep
REM  Runs the alert scan first (so StuckAlert rows are current), then emails
REM  each stage's TRACKER users a digest of invoices stuck past their stage
REM  threshold (invoice number + vendor name). Pre-audit FULL holds are skipped,
REM  and a re-notify cooldown (TRACKER_ALERT_EMAIL_COOLDOWN_HOURS, default 24h)
REM  stops repeat spam. Idempotent - safe to run on a schedule.
REM
REM  Run periodically via Task Scheduler, e.g. once an hour:
REM    schtasks /create /tn "Tracker Stuck Emails" /tr "\"<PROJECT>\run_email_stuck_alerts.bat\"" /sc hourly /ru "JIVO\admin" /rp "<password>" /rl HIGHEST /f
REM
REM  IMPORTANT: on the deployment server, set PROJECT/PYTHON below to the ACTUAL
REM  deployed paths (they are NOT the dev-machine paths).
REM ===========================================================================
setlocal
set PROJECT=c:\Users\Mukesh\Desktop\OMS\OMS-Backend
set PYTHON=%PROJECT%\.venv\Scripts\python.exe
set LOGDIR=%PROJECT%\logs
set LOGFILE=%LOGDIR%\stuck_emails.log

REM Ensure the log directory exists.
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%PROJECT%"

REM Everything below is echoed to the console AND appended to the log file.
(
  echo ==========================================================================
  echo [%date% %time%] Stuck-alert email sweep starting...
  "%PYTHON%" manage.py scan_stuck_alerts
  "%PYTHON%" manage.py email_stuck_alerts
  echo [%date% %time%] Stuck-alert email sweep done.
  echo.
) >> "%LOGFILE%" 2>&1

REM Surface just this run's tail on the console when run interactively.
powershell -NoProfile -Command "Get-Content -Path '%LOGFILE%' -Tail 12"
echo(
echo Full log: %LOGFILE%
endlocal

