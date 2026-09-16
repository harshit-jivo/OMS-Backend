@echo off
REM ===========================================================================
REM  PRDO — production order sync
REM
REM  Pulls SAP's Planned production orders into OMS, routes each through the
REM  Workflow Engine, and retires requests SAP has moved out of Planned.
REM  Idempotent — safe to run on a schedule.
REM
REM  Register it to run every 15 minutes. The feed this replaces averaged
REM  3-29 rows a DAY, so JSAP's 60-second cadence bought nothing:
REM
REM    schtasks /create /tn "OMS Production Order Sync" ^
REM      /tr "\"C:\LiveProjects\OMS\Backend\run_production_sync.bat\"" ^
REM      /sc minute /mo 15 /ru SYSTEM /rl HIGHEST /f
REM
REM  PROJECT is THIS script's own directory (%~dp0), not a hardcoded path, so
REM  the same file is correct on the server and in a dev checkout.
REM
REM  EXIT CODES — READ THIS BEFORE "FIXING" A FAILING TASK
REM    0  ran, and saw orders (or has not been silent long enough to worry)
REM    1  SAP unreachable, a row failed, or an order could not be routed
REM    2  SAP returned NOTHING, and has returned nothing for over a day
REM
REM  Code 2 is not noise. JSAP's equivalent feed died on 13 Aug 2026 and its
REM  job reported "The job succeeded" every 60 seconds for 33 days, because
REM  "no rows" and "no connection" looked identical to it. 432 production
REM  orders went through no approval at all. A sweep that finds nothing for a
REM  day is not healthy and must not claim to be.
REM
REM  NOTE the final `exit /b` below. A .bat returns its LAST command's exit
REM  code — ending on `echo` is what made three tracker tasks report success
REM  while doing nothing for weeks.
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

echo [%date% %time%] PRDO production order sync starting...
"%PYTHON%" manage.py sync_production_orders
set RC=%ERRORLEVEL%
echo [%date% %time%] PRDO production order sync finished with exit code %RC%.

REM Propagate the command's exit code so Task Scheduler's Last Result means
REM something. Do NOT add an `echo` after this line.
exit /b %RC%
