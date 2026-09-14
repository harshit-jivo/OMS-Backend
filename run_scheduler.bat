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
set LOGFILE=%LOGDIR%\scheduler.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

cd /d "%PROJECT%"

echo [%date% %time%] SAP sync scheduler starting... >> "%LOGFILE%"
"%PYTHON%" manage.py run_scheduler >> "%LOGFILE%" 2>&1
echo [%date% %time%] SAP sync scheduler exited with code %ERRORLEVEL%. >> "%LOGFILE%"
endlocal
