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

