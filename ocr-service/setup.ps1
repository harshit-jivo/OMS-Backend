# OCR service setup - run on the OMS server, in an ADMIN PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File C:\LiveProjects\OCR\setup.ps1
#
# Before running: install Python 3.12 (64-bit, "Install for all users", do NOT
# add to PATH), and copy main.py and setup.ps1 into C:\LiveProjects\OCR.
#
# Safe to run again: it reuses what is already there and re-registers the task.
# Everything it prints is also saved to C:\LiveProjects\OCR\setup.log.

param(
    [string]$Root = "C:\LiveProjects\OCR",
    [int]$Port = 8014,
    # mobile: measured on this server ~5.5x faster than server (8.5 s vs 48 s
    # for a 100-line invoice photo) with the same key fields read correctly.
    [ValidateSet("server", "mobile")][string]$Models = "mobile",
    # "off" only if PaddlePaddle still fails with a oneDNN error; much slower.
    [ValidateSet("on", "off")][string]$OneDnn = "on",
    [string]$TaskName = "OMS OCR service",
    # Only if Python 3.12 is somewhere other than C:\Program Files\Python312.
    [string]$Python312 = ""
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Root | Out-Null
Start-Transcript -Path (Join-Path $Root "setup.log") -Force | Out-Null

function Step($text) { Write-Host ""; Write-Host "=== $text ===" -ForegroundColor Cyan }
function Fail($text) { Write-Host "FAILED: $text" -ForegroundColor Red; Stop-Transcript | Out-Null; exit 1 }
function Ok($text) { Write-Host "OK: $text" -ForegroundColor Green }
function Check-Native($what) { if ($LASTEXITCODE -ne 0) { Fail "$what (exit code $LASTEXITCODE)" } }

$Py = Join-Path $Root ".venv\Scripts\python.exe"

try {
    # ------------------------------------------------------------------ 0
    Step "0. Pre-checks"
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
             ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { Fail "Run this in an ADMIN PowerShell (needed to register the startup task)." }
    if (-not (Test-Path (Join-Path $Root "main.py"))) { Fail "main.py is not in $Root - copy it there first." }
    Get-ChildItem $Root -File | Unblock-File
    # Python 3.12 by its install path first, so this does not depend on which
    # `py` launcher the machine has; `py -3.12` only as a fallback.
    $Base = $null
    foreach ($candidate in @($Python312, "C:\Program Files\Python312\python.exe")) {
        if ($candidate -and (Test-Path $candidate)) { $Base = $candidate; break }
    }
    if (-not $Base -and (Get-Command py -ErrorAction SilentlyContinue)) {
        # PowerShell 5.1 turns a redirected stderr line into an error, which
        # "Stop" would make fatal; a missing 3.12 is handled just below.
        $ErrorActionPreference = "Continue"
        $found = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        $ErrorActionPreference = "Stop"
        if ($LASTEXITCODE -eq 0 -and $found) { $Base = "$found".Trim() }
    }
    if (-not $Base) {
        Fail "Python 3.12 not found. Install it (64-bit, for all users), or pass -Python312 <path to python.exe>."
    }
    $version = & $Base -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ("$version".Trim() -ne "3.12") { Fail "$Base is Python $version, not 3.12" }
    Ok "admin, main.py present, Python 3.12 at $Base"

    # ------------------------------------------------------------------ 1
    Step "1. Python environment"
    if (-not (Test-Path $Py)) {
        & $Base -m venv (Join-Path $Root ".venv")
        Check-Native "creating the virtual environment"
    }
    & $Py -m pip install --upgrade pip --quiet
    Check-Native "upgrading pip"
    Ok "environment at $Root\.venv"

    # ------------------------------------------------------------------ 2
    Step "2. Installing packages (several minutes the first time)"
    # paddlepaddle below 3.3: 3.3's CPU build fails running the OCR detection
    # model with oneDNN on ("ConvertPirAttribute2RuntimeAttribute not support
    # [pir::ArrayAttribute<pir::DoubleAttribute>]"). pip downgrades an already
    # installed 3.3 to satisfy this.
    & $Py -m pip install "paddlepaddle>=3,<3.3" "paddleocr>=3,<4" pymupdf fastapi "uvicorn[standard]" python-multipart py-cpuinfo
    Check-Native "installing packages"
    & $Py -m pip freeze | Out-File -Encoding ascii (Join-Path $Root "requirements.txt")
    Ok "packages installed; exact versions saved to requirements.txt"

    # ------------------------------------------------------------------ 3
    Step "3. CPU and engine checks"
    & $Py -c "import cpuinfo; f=cpuinfo.get_cpu_info()['flags']; print('avx:', 'avx' in f, ' avx2:', 'avx2' in f, ' avx512f:', 'avx512f' in f); raise SystemExit(0 if 'avx' in f else 1)"
    Check-Native "this CPU has no AVX; PaddlePaddle's CPU build needs it"
    & $Py -c "import paddle; print('paddlepaddle', paddle.__version__); paddle.utils.run_check()"
    Check-Native "PaddlePaddle check"
    Ok "CPU and PaddlePaddle fine"

    # ------------------------------------------------------------------ 4
    Step "4. Models ($Models)"
    $env:OCR_MODELS = $Models
    $ModelDir = Join-Path $Root "models"
    $need = @("PP-OCRv5_${Models}_det", "PP-OCRv5_${Models}_rec")
    $missing = $need | Where-Object { -not (Test-Path (Join-Path $ModelDir $_)) }
    if ($missing) {
        Write-Host "Downloading models (needs internet, a few hundred MB)..."
        Push-Location $Root
        # Importing main builds the OCR pipeline, which downloads what it lacks
        # into this user's profile.
        & $Py -c "import main; print('models loaded')"
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { Fail "downloading / loading the models (exit code $code)" }

        $cache = Join-Path $env:USERPROFILE ".paddlex\official_models"
        if (-not (Test-Path $cache)) { Fail "models not found in $cache after download" }
        New-Item -ItemType Directory -Force $ModelDir | Out-Null
        Copy-Item (Join-Path $cache "*") $ModelDir -Recurse -Force
    }
    Get-ChildItem $ModelDir -Directory | ForEach-Object { Write-Host "  $($_.Name)" }
    $stillMissing = $need | Where-Object { -not (Test-Path (Join-Path $ModelDir $_)) }
    if ($stillMissing) { Fail "models missing from ${ModelDir}: $($stillMissing -join ', ')" }
    Ok "models in $ModelDir (the service runs as SYSTEM and reads them from here, no download)"

    # ------------------------------------------------------------------ 5
    Step "5. Launcher and startup task"
    New-Item -ItemType Directory -Force (Join-Path $Root "logs") | Out-Null
    $launcher = @"
@echo off
rem Started at boot by the "$TaskName" scheduled task (setup.ps1).
set OCR_MODELS=$Models
set OCR_ONEDNN=$(if ($OneDnn -eq "on") { "1" } else { "0" })
set PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port $Port >> "%~dp0logs\ocr.log" 2>&1
"@
    $launcher | Out-File -Encoding ascii (Join-Path $Root "run.cmd")

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    # Stopping the task ends cmd.exe but can leave its python.exe running.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -like "$Root\.venv\*" -and $_.CommandLine -like "*uvicorn*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
    Start-Sleep -Seconds 3
    $inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($inUse) { Fail "port $Port is already in use (PID $($inUse[0].OwningProcess)); stop that first" }

    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\run.cmd`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # ExecutionTimeLimit 0: by default Windows kills a task after 3 days.
    # Priority 4 (normal): a task otherwise runs BELOW normal (7), which starves
    # CPU-bound OCR whenever anything else on the server is busy.
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -Priority 4
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -User "SYSTEM" -RunLevel Highest | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Ok "task '$TaskName' registered (at startup, as SYSTEM) and started"

    # ------------------------------------------------------------------ 6
    Step "6. Waiting for the service (loading models, up to 3 minutes)"
    $health = $null
    for ($i = 0; $i -lt 90; $i++) {
        try { $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5; break }
        catch { Start-Sleep -Seconds 2 }
    }
    if (-not $health) {
        Write-Host "Last lines of $Root\logs\ocr.log:"
        Get-Content (Join-Path $Root "logs\ocr.log") -Tail 30 -ErrorAction SilentlyContinue
        Fail "the service did not answer on port $Port"
    }
    Ok "service up: $($health | ConvertTo-Json -Compress)"

    # ------------------------------------------------------------------ 7
    Step "7. Self-test: OCR a generated image"
    $sample = Join-Path $Root "selftest.png"
    & $Py -c "from PIL import Image, ImageDraw, ImageFont; im=Image.new('RGB',(900,220),'white'); d=ImageDraw.Draw(im); f=ImageFont.truetype('arial.ttf',48); d.text((30,30),'TAX INVOICE No. INV-2026-0412',fill='black',font=f); d.text((30,120),'Total Rs. 1,37,500.00',fill='black',font=f); im.save(r'$sample')"
    Check-Native "creating the test image"
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $result = & curl.exe -s -F "file=@$sample" "http://127.0.0.1:$Port/ocr"
    Check-Native "calling /ocr"
    $watch.Stop()
    Write-Host $result
    Write-Host ("took {0:N1} s" -f $watch.Elapsed.TotalSeconds)
    if ($result -notmatch "INV-2026-0412") { Fail "the text did not come back as expected (see above)" }
    Ok "OCR works"

    Step "DONE"
    Write-Host "Service: http://127.0.0.1:$Port  (POST /ocr with a file, GET /health)"
    Write-Host "Logs:    $Root\logs\ocr.log"
    Write-Host "Try a real bill:  curl.exe -F `"file=@C:\path\to\bill.pdf`" http://127.0.0.1:$Port/ocr"
}
catch {
    Fail $_.Exception.Message
}
Stop-Transcript | Out-Null
