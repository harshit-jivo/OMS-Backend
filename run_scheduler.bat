@echo off
REM ===========================================================================
REM  OMS SAP-sync scheduler worker  (Phase 5.1)
REM
REM  Runs the APScheduler worker in its OWN process, separate from the web
REM  server. Until now the scheduler was started from inside Django's app
REM  registry, which meant it started once per web worker in development and
REM  never at all in production. This is the only supported way to run it.
REM
REM  Unlike the other .bat files here, this one does NOT exit — it is a
REM  long-running service, not a periodic sweep. Register it to start at boot
REM  and to restart on failure:
REM
REM    schtasks /create /tn "OMS SAP Sync Scheduler" ^
REM      /tr "\"<PROJECT>\run_scheduler.bat\"" ^
REM      /sc onstart /ru "JIVO\admin" /rp "<password>" /rl HIGHEST /f
REM
REM  Starting a second copy is safe: it fails immediately with a non-zero exit
REM  because the first one holds a Postgres advisory lock. That is what makes
REM  an overlapping restart harmless.
REM
REM  To check the configuration without holding the process:
REM    manage.py run_scheduler --once
REM
REM  IMPORTANT: on the deployment server, set PROJECT/PYTHON below to the ACTUAL
REM  deployed paths (they are NOT the dev-machine paths).
REM ===========================================================================
setlocal
set PROJECT=c:\Users\Mukesh\Desktop\OMS\OMS-Backend
set PYTHON=%PROJECT%\.venv\Scripts\python.exe
set LOGDIR=%PROJECT%\logs
set LOGFILE=%LOGDIR%\scheduler.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%PROJECT%"

echo [%date% %time%] SAP sync scheduler starting... >> "%LOGFILE%"
"%PYTHON%" manage.py run_scheduler >> "%LOGFILE%" 2>&1
echo [%date% %time%] SAP sync scheduler exited with code %ERRORLEVEL%. >> "%LOGFILE%"
endlocal
