# OMS Production Setup (Windows server)

A runbook to follow **on the OMS production server, 138.252.101.118**, when
setting up, updating or fixing:

- **Part A:** the OMS backend (Django)
- **Part B:** the OMS frontend (React)
- **Part C:** the OMS scheduled jobs
- **Part D:** the OCR service, and the payment-proof (UTR) reading that uses it
- **Part E:** releasing a new OMS version

Use an **admin** PowerShell for everything. No password or secret is written
in this file: only setting names, and values that are not sensitive.

The OCR part was run on .118 during its first setup (September 2026), and the
outputs quoted are real ones from that server. For a Docker/Linux deployment
instead, see `DEPLOYMENT.md` in the repo root.

---

## 0. The pieces and where they live

```
Browser
  |
  |-- OMS frontend (React build, dist\)                Part B
  |
  '-- OMS backend (Django)  :8000                      Part A
        C:\LiveProjects\OMS\Backend
        |-- PostgreSQL (OMS's own data)   DB_HOST:5432
        |-- SAP HANA (read) + Service Layer (write)
        |-- SAP / JSAP SQL Server
        |-- NIC e-Invoice / e-Way Bill
        |-- Crystal bill print            :8008   (IIS, this server)
        |-- SAP attachment files          :8012   (this server)
        '-- OCR service                   :8014   (127.0.0.1 only)  Part D
              C:\LiveProjects\OCR

Scheduled tasks (Windows Task Scheduler)            Part C
  SAP sync scheduler, tracker alerts / e-mails, JSAP sync,
  production-order sync, SAP-saved sync, auto-IRN
```

| Thing | Where |
|---|---|
| Backend code | `C:\LiveProjects\OMS\Backend` (the `OMS-Backend` git repo) |
| Backend Python | `C:\LiveProjects\OMS\Backend\.venv` (Python 3.14) |
| Backend settings | `C:\LiveProjects\OMS\Backend\.env` (never committed) |
| Backend logs | `C:\LiveProjects\OMS\Backend\logs\` |
| Frontend code | the `OMS-Frontend` git repo; the built site is its `dist\` folder |
| OCR service | `C:\LiveProjects\OCR`, installed from `ocr-service\` in the backend repo |
| OCR Python | `C:\LiveProjects\OCR\.venv` (Python 3.12, separate on purpose) |

---

# Part A: OMS backend

## A1. Requirements

- **Python 3.14**, 64-bit. The backend's environment is built on 3.14.2.
- **Git**, to pull the code.
- **Network access from this server** to:

  | Service | Setting in `.env` | Port |
  |---|---|---|
  | PostgreSQL | `DB_HOST` / `DB_PORT` | 5432 |
  | SAP HANA | `HANA_DB_HOST` / `HANA_DB_PORT` | as set |
  | SAP Service Layer | `HANA_SERVICE_LAYER_URL` | usually 50000 |
  | SAP SQL Server | `SAP_DB_HOST` / `SAP_DB_PORT` | 1433 |
  | JSAP | `JSAP_API_BASE`, `JSAP_DB_*` | as set |
  | NIC e-Invoice / e-Way Bill | `EINV_BASE_URL(S)`, `EWB_BASE_URLS` | 443 |
  | QR / attachment shares on .52 | `EINV_QR_*` | SMB 445 (private IP 10.10.101.52) |
  | Crystal bill print | `CRYSTAL_URL` | 8008 |

## A2. Get the code

First time:

```powershell
New-Item -ItemType Directory -Force C:\LiveProjects\OMS
Set-Location C:\LiveProjects\OMS
git clone https://github.com/harshit-jivo/OMS-Backend.git Backend
```

Update an existing copy to the branch being released:

```powershell
Set-Location C:\LiveProjects\OMS\Backend
git status          # must be clean: do not pull over local edits
git fetch
git checkout <branch>
git pull
```

## A3. Python environment

```powershell
Set-Location C:\LiveProjects\OMS\Backend
py -3.14 -m venv .venv                     # first time only
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Re-run the last line whenever `requirements.txt` changes.

## A4. The `.env` file

`C:\LiveProjects\OMS\Backend\.env` holds every credential OMS has. It is
gitignored. Copy it from the current server, never from git.

**Required.** Django will not start without these:

```
DB_NAME  DB_USER  DB_PASSWORD  DB_HOST  DB_PORT
HANA_DB_HOST  HANA_DB_PORT  HANA_DB_OIL_NAME  HANA_DB_USER  HANA_DB_PASSWORD
HANA_SERVICE_LAYER_URL  HANA_USERNAME  HANA_PASSWORD
SAP_DB_HOST  SAP_DB_NAME  SAP_DB_USER  SAP_DB_PASSWORD
SAP_APPROVER_USER  SAP_APPROVER_PASSWORD
CRYSTAL_URL  JSAP_API_BASE
```

**Production must-haves.** The defaults are wrong for a server:

| Key | Production value | Why |
|---|---|---|
| `DEBUG` | `false` | Turns on the security headers. With `true`, any host is accepted. |
| `SECRET_KEY` | a long random key | Required when `DEBUG=false`: Django refuses to start without it. Signs sessions and every login token. |
| `ALLOWED_HOSTS` | every name/IP users type, e.g. `oms.jivo.in,138.252.101.118,10.10.101.118,127.0.0.1,localhost` | A missing one gives "DisallowedHost" (400) on every request |
| `CSRF_TRUSTED_ORIGINS` | e.g. `https://oms.jivo.in` | Needed for the Django admin login over HTTPS |
| `CORS_ALLOWED_ORIGINS` | the frontend's origin(s), e.g. `https://oms.jivo.in,http://oms.jivo.in` | The browser blocks API calls from any origin not listed |
| `SECURE_SSL_REDIRECT` | `true` only behind HTTPS | With `DEBUG=false` it defaults to `true` and redirects every plain-HTTP request to `https://`. If OMS is served over plain HTTP, set `false`. |

To generate a `SECRET_KEY`:

```powershell
.\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

Changing `SECRET_KEY` logs every user out. Do it in a quiet window.

**Feature keys**, grouped. Set the ones for the features in use:

| Feature | Keys |
|---|---|
| Company schemas | `HANA_DB_NAME`, `HANA_DB_BEVERAGE_NAME`, `HANA_DB_MART_NAME`, `HANA_DB_TEST_NAME`, `HANA_BEVERAGE_COMPANY_DB`, `HANA_COMPANY_DB_BEVERAGES` |
| HANA tuning | `HANA_CONNECT_TIMEOUT`, `HANA_READ_TIMEOUT`, `HANA_SSL_VERIFY`, `HANA_WAREHOUSE_CODE` |
| Sales-order SAP user | `SALES_ORDER_USER`, `SALES_ORDER_PASSWORD` |
| JSAP database | `JSAP_DB_HOST`, `JSAP_DB_PORT`, `JSAP_DB_NAME`, `JSAP_DB_USER`, `JSAP_DB_PASSWORD`, `OMS_JSAP_USER_ID` |
| e-Invoice (Oil / Beverages) | `EINV_BASE_URL(S)`, `EINV_AUTH_PATH`, `EINV_IRN_PATH`, `EINV_CLIENT_ID`, `EINV_CLIENT_SECRET`, `EINV_USERNAME`, `EINV_PASSWORD`, `EINV_GSTIN(S)`, per-GSTIN `EINV_<GSTIN>_USERNAME/PASSWORD`, `EINV_PUBLIC_KEY_PATH`, `EINV_AUTO_GENERATE`, `EINV_SAP_WRITEBACK`, `EINV_MIRROR_HANA` |
| e-Invoice (Mart) | `MART_EINV_CLIENT_ID`, `MART_EINV_CLIENT_SECRET`, `MART_EINV_USERNAME`, `MART_EINV_PASSWORD`, `MART_EINV_GSTIN`, `MART_EINV_PUBLIC_KEY_PATH` |
| e-Invoice QR files | `EINV_QR_ROOT`, `EINV_QR_SAVE_DIR`, `EINV_QR_SAVE_DIR_OIL/_BEVERAGE/_MART`, `EINV_QR_FILENAME`, `EINV_QR_SMB_USERNAME`, `EINV_QR_SMB_PASSWORD` |
| e-Way Bill | `EWB_BASE_URLS`, `EWB_AUTH_PATH`, `EWB_API_PATH` |
| E-mail | `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`, `TRACKER_ALERT_EMAIL_COOLDOWN_HOURS` |
| SAP attachments (POs / bills) | `SAP_ATTACHMENT_FILES_URL`, `SAP_ATTACHMENT_COMPANY_OIL`, `SAP_ATTACHMENT_COMPANY_BEVERAGES`, `SAP_ATTACHMENT_COMPANY_MART` (see D4.2) |
| OCR for payment proofs | `OCR_SERVICE_URL`, `OCR_SERVICE_TIMEOUT` (see D4.2) |
| HSTS (only once HTTPS is solid) | `SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` |

The NIC e-invoice private keys go in `C:\LiveProjects\OMS\Backend\secrets\`,
the folder the `*_PUBLIC_KEY_PATH` keys point into. It is never committed.

Gotcha when checking `.env`: it mixes `KEY=value` and `KEY = value`. Both
work, but a search for `^KEY=` misses the spaced form. To list the key names:

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[A-Z_][A-Z0-9_]*\s*=' } | ForEach-Object { ($_ -split '=')[0].Trim() } | Sort-Object -Unique
```

> **Security note.** `SECRET_KEY`, `SAP_DB_PASSWORD` and `JSAP_DB_PASSWORD`
> have been in git history in the past. If they have not been rotated since,
> rotate them, and update `.env` on every server that uses them.

## A5. Checks before starting

```powershell
Set-Location C:\LiveProjects\OMS\Backend
New-Item -ItemType Directory -Force logs, secrets | Out-Null

# Settings sanity check: with DEBUG=false this also lists security warnings
.\.venv\Scripts\python.exe manage.py check --deploy

# Migrations: PREVIEW first. This lists unapplied migrations and changes nothing.
.\.venv\Scripts\python.exe manage.py showmigrations --plan | Select-String '\[ \]'
```

Apply migrations only after someone has read what they do. This is the live
database:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
```

Static files, for the Django admin pages (served by whitenoise from
`staticfiles\`):

```powershell
.\.venv\Scripts\python.exe manage.py collectstatic --noinput
```

## A6. How the backend is served on this server

The backend answers on **port 8000**. *How* it is kept running on .118 (a
console, a scheduled task, IIS, or a Windows service) was not recorded when
this runbook was written. Find out once, and write it in the table below.

```powershell
# 1. What is listening on 8000, and its full command line
$p = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId = $p" | Select-Object ProcessId, Name, ExecutablePath, CommandLine, ParentProcessId

# 2. Its parent: explorer/cmd = a console someone started; svchost = a task or service; w3wp = IIS
$pp = (Get-CimInstance Win32_Process -Filter "ProcessId = $p").ParentProcessId
Get-CimInstance Win32_Process -Filter "ProcessId = $pp" | Select-Object ProcessId, Name, CommandLine

# 3. Any scheduled task or service that starts it
Get-ScheduledTask | Where-Object { ($_.Actions.Execute + ' ' + $_.Actions.Arguments) -match 'manage.py|runserver|waitress|OMS' } | Select-Object TaskName, State
Get-Service | Where-Object { $_.DisplayName -match 'OMS|Django|python' } | Select-Object Name, Status, DisplayName

# 4. IIS sites, if IIS serves it
Import-Module WebAdministration -ErrorAction SilentlyContinue; Get-Website | Select-Object Name, State, PhysicalPath, @{n='Bindings';e={$_.bindings.Collection.bindingInformation -join ', '}}
```

| | Record it here |
|---|---|
| Started by | *(console / scheduled task "…" / service "…" / IIS site "…")* |
| Command | *(from step 1)* |
| How to restart it | *(…)* |

Whatever runs it must start with its working directory at
`C:\LiveProjects\OMS\Backend` and use `.venv\Scripts\python.exe`.

## A7. Check the backend

```powershell
# Reachable at all: a GET on the POST-only login route answers 405
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8000/api/auth/login/

# Database reachable: a real login attempt with wrong details must be 401, not 500
try {
    Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/auth/login/ -ContentType 'application/json' -Body '{"username":"x","password":"y"}'
} catch {
    $_.Exception.Response.StatusCode.value__
    $_.ErrorDetails.Message
}
```

The second check should print `401` and `{"success":false,"message":"Login failed",...}`.
A 500 means the database or a required setting is wrong: see `logs\`.

---

# Part B: OMS frontend

## B1. Requirements

Node.js 24 (built with v24.13.0) and npm, on whichever machine builds it.

## B2. Build settings

Set in the frontend's `.env` (or `.env.production` for release builds):

| Key | Value | Notes |
|---|---|---|
| `VITE_API_BASE_URL` | the backend's `/api` URL **as users' browsers reach it**, e.g. `https://oms.jivo.in/api` | Required: the build refuses to start without it. Baked into the build, so a change needs a rebuild. |
| `VITE_PUBLIC_APP_URL` | e.g. `https://oms.jivo.in` | Base of the HAIS device QR printed on stickers. Unset, a sticker printed from a laptop carries a dead `localhost` link. |
| `VITE_API_VERSION` | leave unset | `v1` only once the server serves `/api/v1/` |
| `VITE_BUILD_NUMBER` | in `.env.production`, raised each release | See `docs/RELEASE.md` → "React web release process" |

The frontend's origin must also be in the backend's `CORS_ALLOWED_ORIGINS`
(A4).

## B3. Build

```powershell
Set-Location <path to OMS-Frontend>
git pull
npm ci
npm run build          # type-checks, then writes dist\
```

## B4. Deploy

Publish the contents of `dist\` to the web host that serves the site. The host
must send `index.html` for any path that is not a file (single-page app
fallback). Without that, a deep link such as `/Advance_Payment_Approval` or a
browser refresh gives a 404.

*How the frontend is served on .118 was not recorded either.* Find the
process on port 80 / 443 with the step 1 commands in A6 (replace `8000` with
`80` or `443`), and step 4 lists IIS sites. Then record it:

| | Record it here |
|---|---|
| Served by | *(IIS site "…" / nginx / other)* |
| Site folder | *(…)* |
| How to deploy a build | *(copy `dist\*` to … )* |

---

# Part C: Scheduled jobs

Each job is a `.bat` file in `C:\LiveProjects\OMS\Backend`. Each one finds its
own folder and `.venv`, so the same file works on any server. Each is
idempotent: a repeated or overlapping run is safe.

| Task name | File | Runs | What it does |
|---|---|---|---|
| OMS SAP Sync Scheduler | `run_scheduler.bat` | at startup (long-running) | SAP sync scheduler |
| Tracker Stuck Alerts | `run_stuck_alerts.bat` | every 30 min | flags invoices stuck past their stage's limit |
| Tracker Stuck Emails | `run_email_stuck_alerts.bat` | hourly | scans, then e-mails each stage's users their stuck invoices; log `logs\stuck_emails.log` |
| Tracker JSAP Sync | `run_jsap_sync.bat` | every 10 min | copies JSAP budget approvals / rejections onto tracker invoices |
| OMS Production Order Sync | `run_production_sync.bat` | every 15 min | production orders from SAP |
| Tracker SAP Saved Sync | `run_sap_saved_sync.bat` | daily 00:00 | invoices already saved in SAP; log `logs\sap_saved_sync.log` |
| (Auto-IRN) | `run_auto_irn.bat` | every 15 min | generates missing IRNs for recent Oil and Beverages invoices |

See what is registered now:

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -match 'OMS|Tracker|IRN' } |
    Select-Object TaskName, State, @{n='Runs';e={$_.Actions.Execute + ' ' + $_.Actions.Arguments}}
```

Register a missing one. Run it as **SYSTEM**, not as a logged-in user: a task
tied to a console gets killed mid-run by a Ctrl+C in that console.

```powershell
$B = "C:\LiveProjects\OMS\Backend"
schtasks /create /tn "Tracker Stuck Alerts"      /tr "`"$B\run_stuck_alerts.bat`""       /sc minute /mo 30 /ru SYSTEM /rl HIGHEST /f
schtasks /create /tn "Tracker Stuck Emails"      /tr "`"$B\run_email_stuck_alerts.bat`"" /sc hourly        /ru SYSTEM /rl HIGHEST /f
schtasks /create /tn "Tracker JSAP Sync"         /tr "`"$B\run_jsap_sync.bat`""          /sc minute /mo 10 /ru SYSTEM /rl HIGHEST /f
schtasks /create /tn "OMS Production Order Sync" /tr "`"$B\run_production_sync.bat`""    /sc minute /mo 15 /ru SYSTEM /rl HIGHEST /f
schtasks /create /tn "Tracker SAP Saved Sync"    /tr "`"$B\run_sap_saved_sync.bat`""     /sc daily /st 00:00 /ru SYSTEM /rl HIGHEST /f
```

- **`run_scheduler.bat`:** its header registers it `/sc onstart` under a
  domain account (`JIVO\admin`). Follow the header, since that account may
  need network rights that SYSTEM lacks.
- **`run_auto_irn.bat`:** check with the e-invoice owner before enabling it,
  as it files real IRNs with NIC.
- **Before enabling the e-mail job** on a new server, dry-run it to see who
  would be e-mailed:
  `.\.venv\Scripts\python.exe manage.py email_stuck_alerts --dry-run`

---

# Part D: OCR service and payment proofs

## D1. What it does

```
Payments Approval -> approved request -> "Record Payment" -> upload proof
   |
   v
OMS backend   POST /api/advance-payments/payment-proof/
   |-- PDF with text, Excel (.xlsx/.xls), CSV  -> read by Django itself (instant)
   '-- photo / screenshot / scanned PDF page  -> OCR service, http://127.0.0.1:8014/ocr
```

- The OCR service is FastAPI + PaddleOCR, in its **own Python 3.12**
  environment. PaddlePaddle has no Python 3.14 build, so it cannot share the
  backend's.
- It listens on **127.0.0.1 only**, so only programs on this server can call
  it. No firewall rule is needed, and none should be added.
- It starts at boot as the scheduled task **"OMS OCR service"**, running as
  SYSTEM, and restarts itself if it crashes.
- The backend finds the UTR (bank reference) in the text and checks it
  against the payment line: amount, account paid to, invoice numbers. It works
  for any bank; there are no per-bank templates.

| Thing | Where |
|---|---|
| Source (in the backend repo) | `C:\LiveProjects\OMS\Backend\ocr-service\` |
| Installed service | `C:\LiveProjects\OCR` |
| Code | `C:\LiveProjects\OCR\main.py` |
| Installer / updater | `C:\LiveProjects\OCR\setup.ps1` |
| Speed test | `C:\LiveProjects\OCR\bench.py` |
| Models (no internet needed after setup) | `C:\LiveProjects\OCR\models\` |
| Service log | `C:\LiveProjects\OCR\logs\ocr.log` |
| Last setup log | `C:\LiveProjects\OCR\setup.log` |
| Exact package versions | `C:\LiveProjects\OCR\requirements.txt` |
| Python 3.12 (base only) | `C:\Program Files\Python312\` |
| Port | 8014, on 127.0.0.1 |

## D2. Server requirements (as checked on .118)

| Need | .118 has |
|---|---|
| CPU with AVX | 2 x Intel Xeon Platinum 8269CY (AVX, AVX2, AVX-512) |
| RAM, about 2 GB free | 60 GB |
| Disk, about 3 GB free on C: | 282 GB free |
| Port 8014 free | yes |

```powershell
Get-CimInstance Win32_ComputerSystem | Select-Object @{n='RAM_GB';e={[math]::Round($_.TotalPhysicalMemory/1GB)}}
Get-CimInstance Win32_Processor | Select-Object Name
Get-PSDrive C | Select-Object @{n='Free_GB';e={[math]::Round($_.Free/1GB)}}
Get-NetTCPConnection -LocalPort 8014 -ErrorAction SilentlyContinue   # no output = free
```

## D3. First-time setup

### D3.1 Install Python 3.12 (next to Python 3.14)

A virtual environment is built *from* an installed Python, so 3.12 itself is
installed once. It goes into its own folder and is **not** added to PATH, so
`python` still means 3.14 and nothing else changes. The `py` launcher already
exists (from 3.14), so the installer leaves it alone (`Include_launcher=0`).

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
$installer = "$env:TEMP\python-3.12.10-amd64.exe"
Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $installer -UseBasicParsing
Start-Process $installer -Wait -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=0','Include_launcher=0','Include_test=0'
```

In a **new** PowerShell window:

```powershell
& "C:\Program Files\Python312\python.exe" --version   # Python 3.12.10
python --version                                      # still 3.14.x
```

If the download 404s, take the latest **3.12** "Windows installer (64-bit)"
link from python.org and change the version in both places.

### D3.2 Copy the service files in

After pulling the backend (A2), the files are in its `ocr-service\` folder:

```powershell
New-Item -ItemType Directory -Force C:\LiveProjects\OCR | Out-Null
Copy-Item C:\LiveProjects\OMS\Backend\ocr-service\main.py, `
          C:\LiveProjects\OMS\Backend\ocr-service\setup.ps1, `
          C:\LiveProjects\OMS\Backend\ocr-service\bench.py `
          C:\LiveProjects\OCR\ -Force

# Both must print a line; nothing = an old copy
Select-String -Path C:\LiveProjects\OCR\main.py  -Pattern "def _rows"         | Select-Object -First 1
Select-String -Path C:\LiveProjects\OCR\setup.ps1 -Pattern 'Models = "mobile"' | Select-Object -First 1
```

### D3.3 Run the installer

```powershell
powershell -ExecutionPolicy Bypass -File C:\LiveProjects\OCR\setup.ps1
```

The first run takes several minutes and needs internet access **this once**.
It stops at the first thing that fails and says which step it was.

| Step | What it does |
|---|---|
| 0 | Checks admin rights, that `main.py` is present, and finds Python 3.12 |
| 1 | Creates `C:\LiveProjects\OCR\.venv` |
| 2 | Installs PaddlePaddle (**below 3.3**, see D7.1), PaddleOCR 3.x, PyMuPDF, FastAPI, Uvicorn; saves `requirements.txt` |
| 3 | Checks the CPU has AVX, and that PaddlePaddle works |
| 4 | Downloads the OCR models and copies them into `models\` |
| 5 | Writes `run.cmd` and registers the startup task "OMS OCR service" |
| 6 | Waits for `http://127.0.0.1:8014/health` to answer |
| 7 | Self-test: OCRs a generated image of "TAX INVOICE No. INV-2026-0412" |

A good run ends with:

```
=== 7. Self-test: OCR a generated image ===
{"pages":[{"page":1,"source":"ocr","text":"TAX INVOICE No. INV-2026-0412\nTotal Rs. 1,37,500.00",...}]}
took 3.4 s
OK: OCR works

=== DONE ===
```

Options, all optional:

| Option | Default | Use |
|---|---|---|
| `-Models mobile` / `-Models server` | `mobile` | mobile is about 5.5x faster, see D6.3 |
| `-OneDnn on` / `-OneDnn off` | `on` | `off` only if PaddlePaddle fails with a oneDNN error; much slower |
| `-Port 8014` | `8014` | |
| `-Python312 "D:\...\python.exe"` | `C:\Program Files\Python312\python.exe` | only if 3.12 is installed elsewhere |

### D3.4 Check the service

```powershell
curl.exe http://127.0.0.1:8014/health
```

Expected:

```
{"status":"ok","models":"mobile","onednn":true,"paddle":"3.2.2","max_side":2400,"threads":16,"batch":16}
```

- `models` must be `mobile`.
- `paddle` must be **3.2.x**. 3.3 breaks OCR, see D7.1.
- If `batch` is missing, the service is running an old `main.py`.

### D3.5 Test with real SAP attachments

The SAP attachment file service (port 8012) runs on this server, so real
files can be pulled from it. This downloads three known ones and OCRs each.
It copies each file to a plain name first, because `curl -F` reads a comma in
a file name as "several files".

```powershell
$work = "C:\LiveProjects\OCR\samples"
New-Item -ItemType Directory -Force $work | Out-Null
$samples = @(
    @{ company = 3; name = "WhatsApp Image 2025-02-06 at 1.01.14 PM.jpeg" },  # Mart bill 431
    @{ company = 1; name = "DocScanner Sep 17, 2026 12-39 PM.pdf" },          # Oil bill 51897
    @{ company = 2; name = "okkh.pdf" }                                       # Beverages bill 139
)
foreach ($s in $samples) {
    $zip = Join-Path $work "download.zip"
    curl.exe -s -f -o $zip ("http://127.0.0.1:8012/files/" + [uri]::EscapeDataString($s.name) + "?company=" + $s.company)
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not download $($s.name)" -ForegroundColor Red; continue }
    Expand-Archive $zip -DestinationPath $work -Force
    $plain = Join-Path $work ("sample" + [IO.Path]::GetExtension($s.name))
    Copy-Item (Join-Path $work $s.name) $plain -Force
    Write-Host ""; Write-Host "===== $($s.name) =====" -ForegroundColor Cyan
    $watch = [Diagnostics.Stopwatch]::StartNew()
    curl.exe -s -F "file=@$plain" http://127.0.0.1:8014/ocr
    Write-Host ""; Write-Host ("took {0:N1} s" -f $watch.Elapsed.TotalSeconds) -ForegroundColor Yellow
}
```

What to expect, with the mobile models:

- **WhatsApp invoice photo:** about 9 s. It should read invoice no.
  `2024-25/DL086`, GSTIN `07AAGCG3326A1Z5`, total `2,88,746.00` and bank a/c
  `50200057911744`.
- **Oil DocScanner PDF:** about 9 s, `"source":"ocr"`. It should read bill no.
  `LDH005266` and total `20358.00`.
- **Beverages PDF:** `"source":"ocr"`. It's a printed e-mail with an image in
  it, so the whole page is OCR'd.

Each OCR'd page returns `text` (line by line), `rows` (grouped by position on
the page; what OMS reads), `confidence`, `size` and `seconds`.

To OCR any other file, use a real path in quotes, with no comma in the name:

```powershell
curl.exe -F "file=@C:\path\to\file.pdf" http://127.0.0.1:8014/ocr
```

## D4. Connect the backend to OCR

### D4.1 Backend code

The payment-proof code is part of the backend (Part A): pulling the release
brings it. The files involved, for reference:

| File | What it is |
|---|---|
| `advance_payment\services\proof_reader.py` | reads PDF / Excel / CSV / HTML-as-xls; sends photos and scans to OCR |
| `advance_payment\services\payment_proof.py` | finds the UTR and checks amount / account / invoice |
| `advance_payment\services\attachment_files.py` | fetches SAP attachments via port 8012 |
| `advance_payment\views.py`, `urls.py` | `POST /api/advance-payments/payment-proof/`, `GET /document-attachment/` |
| `advance_payment\tests.py` | 20 tests |
| `OMS\settings.py` | reads the keys below |

No database migrations are involved.

### D4.2 Backend `.env` keys

Add to `C:\LiveProjects\OMS\Backend\.env`, skipping any line already there:

```
OCR_SERVICE_URL=http://127.0.0.1:8014
SAP_ATTACHMENT_FILES_URL=http://138.252.101.118:8012/files
SAP_ATTACHMENT_COMPANY_OIL=1
SAP_ATTACHMENT_COMPANY_BEVERAGES=2
SAP_ATTACHMENT_COMPANY_MART=3
```

| Key | Default | Meaning |
|---|---|---|
| `OCR_SERVICE_URL` | empty | where the OCR service is. **Empty:** PDFs with text, Excel and CSV still work; photos and scans are refused with a clear message |
| `OCR_SERVICE_TIMEOUT` | `180` | seconds to wait for OCR |
| `SAP_ATTACHMENT_FILES_URL` | empty | the SAP attachment file service |
| `SAP_ATTACHMENT_COMPANY_*` | empty | that service's company numbers: 1 Oil, 2 Beverages, 3 Mart |

### D4.3 Check the connection

```powershell
Set-Location C:\LiveProjects\OMS\Backend
.\.venv\Scripts\python.exe manage.py test advance_payment        # Ran 20 tests ... OK
.\.venv\Scripts\python.exe manage.py shell -c "from advance_payment.services import proof_reader; r = proof_reader.read(open(r'C:\LiveProjects\OCR\samples\sample.jpeg','rb').read(), 'sample.jpeg'); print(r['source'], len(r['rows'])); print(r['rows'][:5])"
```

The shell command should print `ocr`, a row count, and the first five rows.
The sample is an invoice, not a bank proof, so there is no UTR in it; this
only proves Django on this server can reach the OCR service.

Then restart the backend (A6) so it loads the new `.env`.

### D4.4 End-to-end check

1. Open **Payments Approval**, go to **Approved** requests, and open one paid
   by UPI / NEFT / RTGS / IMPS.
2. The **Record Payment** card is there, with one row per transfer line.
3. Upload a real bank advice, screenshot or statement and click **Read proof**.
4. It shows the UTR, how the file was read, and the checks:
   - **Amount:** matches this line?
   - **Account:** paid to the account on the request?
   - **Invoice:** invoice number in the remarks?
5. Correct the UTR if needed, or pick another reference found in the file,
   then click **Record UTR**.

> The approval screen still keeps its data in the browser (sample data), so
> recorded UTRs are lost on page reload until requests are saved to the
> database.

## D5. What proofs are accepted

| Upload | How it is read | Speed |
|---|---|---|
| Net-banking PDF (has text) | directly, by Django | under 1 s |
| Excel `.xlsx` / `.xls`, including banks' "xls" that is really an HTML table | directly | under 1 s |
| CSV | directly | under 1 s |
| Scanned PDF, or PDF pages that are mostly an image | OCR, page by page | about 9 s a page |
| Photo / screenshot (JPEG, PNG, WebP) | OCR | a few seconds (screenshot) to about 10 s (full page) |
| iPhone HEIC | refused: share it as JPEG instead | |
| Password-protected PDF | refused: save a copy without the password | |

Limits: 15 MB per file, 30 pages per PDF.

How the UTR is recognised, for any bank:

| Rail | Reference shape | Example |
|---|---|---|
| RTGS | 22 characters: bank code + `R` + 17 | `HDFCR52026092312345678` |
| NEFT | 16 or 22 characters: bank code + 12, bank code + `N` + 17, or `N` + 15 | `SBIN126267123456` |
| IMPS / UPI | 12-digit RRN | `626612345678` |

It also reads labelled values ("UTR No.", "RRN", "Transaction ID", "Ref
No."), including a label with its value on the next line, as UPI screenshots
show it. Known account numbers (the payee's and the company's own) and IFSC
codes are never taken as a UTR.

## D6. OCR day-to-day

### D6.1 Status, logs, restart

```powershell
curl.exe http://127.0.0.1:8014/health
Get-ScheduledTask -TaskName "OMS OCR service" | Select-Object State
Get-Content C:\LiveProjects\OCR\logs\ocr.log -Tail 40
Stop-ScheduledTask  -TaskName "OMS OCR service"
Start-ScheduledTask -TaskName "OMS OCR service"      # wait ~30 s, then /health
```

The model load at startup normally takes 10 to 16 s. Once, right after the
models were first downloaded, it took about 2 minutes; that was a one-off.

`logs\ocr.log` grows over time. Trim or delete it while the task is stopped.

### D6.2 Update the OCR service

```powershell
Copy-Item C:\LiveProjects\OMS\Backend\ocr-service\main.py, `
          C:\LiveProjects\OMS\Backend\ocr-service\setup.ps1, `
          C:\LiveProjects\OMS\Backend\ocr-service\bench.py `
          C:\LiveProjects\OCR\ -Force
powershell -ExecutionPolicy Bypass -File C:\LiveProjects\OCR\setup.ps1
curl.exe http://127.0.0.1:8014/health
```

Setup reuses what is installed and just restarts the service with the new
code. Always update through `setup.ps1`: stopping the task alone does not
always stop its Python process, and setup handles that.

### D6.3 Measure speed, or change models

`bench.py` OCRs files outside the service, each one twice (the first run
includes a one-off warm-up):

```powershell
Set-Location C:\LiveProjects\OCR
.\.venv\Scripts\python.exe bench.py samples\sample.jpeg samples\sample.pdf

$env:OCR_MODELS = "server"            # try other models without touching the service
.\.venv\Scripts\python.exe bench.py samples\sample.jpeg
Remove-Item Env:OCR_MODELS
```

Set `$env:BENCH_TEXT = "1"` to print the text read as well, to compare
accuracy.

Measured on .118 (16 CPUs), a 952x1280 photo with 104 lines:

| Models | Time | Key fields |
|---|---|---|
| **mobile** (in use) | **8.5 s** | invoice no., GSTINs, amounts, a/c no., IFSC correct; one character of the date misread |
| server | 48.5 s | same fields correct; IFSC in lower case |

To switch the service, run setup with `-Models server` or `-Models mobile`.

Service settings, written into `run.cmd` by setup, or set by hand for
`bench.py`:

| Variable | Default | |
|---|---|---|
| `OCR_MODELS` | `mobile` | `mobile` or `server` |
| `OCR_ONEDNN` | `1` | `0` turns off Intel acceleration (last resort) |
| `OCR_MAX_SIDE` | `2400` | bigger images are shrunk to this before OCR |
| `OCR_THREADS` | `16` | CPU threads |
| `OCR_BATCH` | `16` | text lines recognised per batch |
| `OCR_MODEL_DIR` | `.\models` | |

## D7. OCR troubleshooting

These are the real problems hit during setup, and their fixes.

### D7.1 `NotImplementedError: ... ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>] ... onednn_instruction.cc`

- **Seen as:** `/health` is fine, but every OCR call fails. In
  `logs\ocr.log` the traceback ends in `paddle_static\runner.py`.
- **Cause:** PaddlePaddle **3.3** on CPU with oneDNN.
- **Fix:** keep PaddlePaddle **below 3.3**. `setup.ps1` pins
  `paddlepaddle>=3,<3.3`, and re-running it downgrades a 3.3 install. Never
  upgrade past 3.2 without re-testing. Only if 3.2 fails the same way, run
  setup with `-OneDnn off`, which works but is several times slower.

### D7.2 `Incomplete string token` at the last line of `setup.ps1`

A stray character (here a backtick `` ` ``) after the last line. Copy a clean
`setup.ps1` from the repo, or remove it in place:

```powershell
$f = "C:\LiveProjects\OCR\setup.ps1"
(Get-Content $f -Raw).TrimEnd().TrimEnd('`').TrimEnd() + "`r`n" | Set-Content $f -Encoding ascii
```

### D7.3 `curl: (26) Failed to open/read local data from file/application`

The path after `file=@` does not exist, is a placeholder, or the file name has
a comma in it. Use a real path, or copy the file to a plain name (D3.5).

### D7.4 OCR very slow (40 s or more per page)

Check `/health`:

- `"models":"server"`: re-run setup, which defaults to mobile.
- no `max_side` / `batch` fields: an old `main.py` is running. Copy the new
  one and re-run setup.

To tell the engine from the service, compare with `bench.py` (D6.3).

### D7.5 `/health` does not answer after setup or a restart

```powershell
Get-Content C:\LiveProjects\OCR\logs\ocr.log -Tail 40
Get-NetTCPConnection -LocalPort 8014 -State Listen -ErrorAction SilentlyContinue
```

- **Port in use:** stop the process it names, then re-run setup.
- **Models missing:** check that `C:\LiveProjects\OCR\models\` holds
  `PP-OCRv5_mobile_det`, `PP-OCRv5_mobile_rec` and
  `PP-LCNet_x1_0_textline_ori`. If not, re-run setup (needs internet once).

The service runs as SYSTEM, which cannot see the admin user's download cache.
That is why the models are copied into `models\`.

### D7.6 Setup step 0 says "Python 3.12 not found"

Install it (D3.1), or pass its path:
`setup.ps1 -Python312 "D:\...\python.exe"`.

### D7.7 In OMS: "This file needs OCR ... OCR is not set up here (OCR_SERVICE_URL)"

`OCR_SERVICE_URL` is missing from the backend `.env`, or the backend was not
restarted after adding it (D4.2).

### D7.8 In OMS: "The OCR service could not be reached" / "answered 500"

The OCR service is down or failing. See D7.5 and D7.1.

### D7.9 In OMS: "No UTR-like reference was found"

The file has no reference in a known shape. Typical causes: an internal
same-bank transfer, a cropped screenshot, or a very blurry photo. Upload the
bank's own advice or statement instead, or type the UTR by hand.

## D8. Remove or roll back OCR

- **Stop using OCR, keep everything else:** remove `OCR_SERVICE_URL` from the
  backend `.env` and restart the backend. PDFs, Excel and CSV still work;
  photos are refused with a message.
- **Remove the OCR service completely:**
  ```powershell
  Stop-ScheduledTask       -TaskName "OMS OCR service"
  Unregister-ScheduledTask -TaskName "OMS OCR service" -Confirm:$false
  Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
      Where-Object { $_.ExecutablePath -like "C:\LiveProjects\OCR\.venv\*" } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  Remove-Item -Recurse -Force C:\LiveProjects\OCR
  ```
  Then uninstall **Python 3.12** from Apps if nothing else uses it. That
  removes only 3.12; 3.14 and the OMS backend are unaffected.

---

# Part E: Releasing a new OMS version

1. **Backend code** (A2): `git status` must be clean, then `git pull` the
   release branch.
2. **Packages** (A3), if `requirements.txt` changed:
   `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`
3. **`.env`:** add any new keys the release notes list (A4, D4.2).
4. **Migrations** (A5): preview with `showmigrations --plan`, have them read,
   then `migrate`.
5. **Static files:** `manage.py collectstatic --noinput`.
6. **Restart the backend** (A6), then check it (A7).
7. **Scheduled jobs** (Part C): register any new `run_*.bat`.
8. **OCR service**, if `ocr-service\` changed: D6.2.
9. **Frontend** (Part B): raise `VITE_BUILD_NUMBER`, build, deploy `dist\`.
10. **Smoke test:** log in, open the pages the release touched, and for this
    release, the end-to-end check in D4.4.

If something goes wrong after a release: `git checkout` the previous commit
in `C:\LiveProjects\OMS\Backend`, restart the backend, and redeploy the
previous frontend build. A migration that already ran is **not** undone by
this; check with whoever wrote it before reversing one.
