# IRN dual write — two QR folders, two tables

What OMS does after every IRN generation once `EINV_MIRROR_UDO` is on, how to
turn it on in production, and what to check afterwards.

Companion to `INVOICE_PRINTING_AND_QR_RUNBOOK.md` and
`OMS_IRN_TO_SAP_UDO_TRANSFER.md` (the one-off backfill this replaces).

No credentials appear in this document. Where a value is secret it is referred
to by its `.env` key only.

---

## 1. The model

An IRN has two independent readers, and neither can use the other's record:

| reader | table | folder it opens |
|---|---|---|
| OMS bill print (`OMS_SP_GST_INVOICE`) | `OMS_IRN_LOG` | the **OMS** folder |
| SAP Crystal layouts (`CRYSTAL_AR_INVOICE_*`) | `@UTL_MDEXTH` | the **SAP** folder |

So each table records the path to its own folder, and both the row and the file
have to be in place for a bill to print with a QR.

```
generate IRN at NIC
  |
  |-- 1a  QR PNG -> OMS folder      ALWAYS. no duplicate check, never skipped.
  |                                 path stored in OMS_IRN_LOG."U_UTL_QRPT"
  |
  |-- 1b  QR PNG -> SAP folder      only if the file is not already there.
  |                                 path stored in @UTL_MDEXTH."U_UTL_QRPT"
  |
  |-- 2a  row    -> OMS_IRN_LOG     gated by EINV_MIRROR_HANA
  |
  '-- 2b  row    -> @UTL_MDEXTH     gated by EINV_MIRROR_UDO   <- the new part
```

### Folders

Filename is the IRN plus `.png`, per `EINV_QR_FILENAME`.

**OMS folder** — must never be short a file; without it OMS cannot print at all.

```
OIL       \\<file-server>\OMS_Attachments\OIL_ATTACHMENTS\Bitmap\<irn>.png
BEVERAGE  \\<file-server>\OMS_Attachments\BEVERAGE_ATTACHMENTS\Bitmap\<irn>.png
MART      \\<file-server>\OMS_Attachments\MART_ATTACHMENTS\Bitmap\<irn>.png
```

**SAP folder** — shared with the SAP add-on, so writes are duplicate-checked.

```
OIL       \\<file-server>\SAP Attachments\Jivo Oil\Bitmaps\<irn>.png
BEVERAGE  \\<file-server>\SAP Attachments\Jivo Beverages\Bitmaps\<irn>.png
MART      \\<file-server>\SAP Attachments\Jivo Mart\Bitmaps\<irn>.png
```

Note `Bitmap` (singular) on the OMS side and `Bitmaps` (plural) on the SAP side.
That asymmetry is pre-existing and easy to mistype.

### Ordering, and why

The OMS write comes first and its failure is logged as an error naming the
consequence. The SAP write comes second and may legitimately do nothing. The
row writes follow, `OMS_IRN_LOG` before `@UTL_MDEXTH`, for the same reason: the
OMS row is what makes the invoice printable at all, and nothing later in the
chain is allowed to put it at risk.

If the OMS file write fails, the row records the SAP path instead of nothing — a
QR in the other folder still prints, a `NULL` never does.

### The duplicate checks

Two different ones, for two different reasons.

**Files** — a single existence check per IRN, not a directory listing (the SAP
folders hold tens of thousands of files; listing them on every generation would
cost more than the write it avoids). Because the filename *is* the IRN, a name
match is the same image, so skipping is safe and avoids rewriting a file a
report may be reading mid-render. If the share is unreachable the check reports
"not present", so the write is attempted and fails loudly rather than being
silently skipped.

**Rows** — `@UTL_MDEXTH` gets a `NOT EXISTS` guard on
`U_UTL_BaseEntry` + `U_UTL_DocType`. **This is not an optimisation.** Two `'S'`
rows for one invoice make the SAP Crystal procs fail with SQL error 305
("single-row query returns more than one row") and the bill does not render at
all. See `OMS_IRN_TO_SAP_UDO_TRANSFER.md` §6.

---

## 2. What to change in `.env` in production

### Required

```ini
# Write the IRN into the SAP add-on's UDO table as well as OMS_IRN_LOG.
EINV_MIRROR_UDO=true
```

That is the only new value that must be set. Everything else already exists or
has a working default.

### Verify, do not assume

| key | must be | why it matters |
|---|---|---|
| `EINV_MIRROR_HANA` | `true` | without it there is no `OMS_IRN_LOG` row to print from |
| `EINV_QR_SAVE_DIR_OIL` / `_BEVERAGE` / `_MART` | the three **SAP** Bitmaps folders | these are what `@UTL_MDEXTH` records |
| `EINV_QR_ROOT` | the OMS attachments root | the OMS folders derive from it |
| `EINV_QR_FILENAME` | `{irn}.png` | the duplicate check matches on this name |
| `EINV_QR_SMB_USERNAME` / `_PASSWORD` | set | the service account cannot reach the shares unaided |
| the three company-DB keys | the **live** schemas | a dev checkout may point a company at a `TEST_*` schema; the mirror writes wherever these point |

That last row is the one most likely to bite. Confirm what the app actually
resolves, rather than reading `.env`:

```bash
python manage.py shell -c "from django.conf import settings as s; \
print(s.HANA_OIL_COMPANY_DB, s.HANA_BEVERAGE_COMPANY_DB, s.HANA_MART_COMPANY_DB)"
```

### Optional

The OMS folders default to `<EINV_QR_ROOT>\<CO>_ATTACHMENTS\Bitmap`. Override
only if they need to live elsewhere:

```ini
EINV_OMS_QR_SAVE_DIR_OIL=
EINV_OMS_QR_SAVE_DIR_BEVERAGE=
EINV_OMS_QR_SAVE_DIR_MART=
```

---

## 3. Pre-flight, before turning it on

Run everything from the application host. A developer workstation cannot reach
the file shares and will report misleading results.

1. **Shares reachable**

   ```bash
   python manage.py test_qr_share
   ```

<<<<<<< HEAD
=======
   > ⚠️ **This only covers three of the six folders.** `test_qr_share` reads
   > `settings.EINV_QR_SAVE_DIRS` and nothing else — i.e. the three **SAP** folders.
   > It never probes `EINV_OMS_QR_SAVE_DIRS`, even though the OMS write is the
   > must-never-fail path. "All 3 QR folder(s) reachable and writable" is **not**
   > the all-clear step 2 asks for. Until the command covers both maps, probe the
   > OMS folders too — same logic, different dict:
   >
   > ```python
   > from django.conf import settings
   > from einvoice.management.commands.test_qr_share import Command
   > cmd = Command()
   > for db, d in settings.EINV_OMS_QR_SAVE_DIRS.items():
   >     print(db, cmd._probe(d))
   > ```

>>>>>>> 8d7af8811f9346fd059666d31a14d64ef9c5a9ca
2. **All six folders exist and are writable.** `MART_ATTACHMENTS\Bitmap` has no
   confirmed historical use — no stored path has ever referenced it — so it is
   the one most likely to be missing. The writer creates the folder if it can,
   but this is now on the must-never-fail path, so confirm rather than discover
   it at the first Mart IRN.

<<<<<<< HEAD
=======
   *(2026-09-19: all six verified reachable and writable on `10.10.101.118`,
   including `MART_ATTACHMENTS\Bitmap`.)*

>>>>>>> 8d7af8811f9346fd059666d31a14d64ef9c5a9ca
3. **Both commands report cleanly**, with the flag still off:

   ```bash
   python manage.py reconcile_udo_mirror      # rows in OMS_IRN_LOG but not @UTL_MDEXTH
   python manage.py repair_qr_files           # PNGs missing from either folder
   ```

   Neither writes anything without `--apply`. Exit 1 means "work to do", exit 2
   means a company or folder could not be read — investigate before proceeding.

   If `repair_qr_files` says a folder is *empty or unreadable* it refuses to act
   rather than concluding that every file is missing. Treat that as a share
   problem, not as a repair backlog.

<<<<<<< HEAD
=======
   > **…unless the folder is legitimately empty.** The guard cannot tell "empty"
   > from "unreadable", so a company that has never generated an OMS IRN trips it
   > on a perfectly healthy share — and takes the whole command to **exit 2**.
   > That is Mart today: 0 signed rows in `OMS_IRN_LOG`, nothing ever written to
   > `MART_ATTACHMENTS\Bitmap`, yet the probe in step 1 writes/reads/deletes there
   > fine. Before treating exit 2 as a share fault, check the row count:
   >
   > ```sql
   > SELECT COUNT(*) FROM "<schema>"."OMS_IRN_LOG" WHERE "U_UTL_IST" = 'S';
   > ```
   >
   > Zero means the message is a false positive. It will keep firing until that
   > company's first IRN lands, so **do not wire exit 2 straight to an alert.**

>>>>>>> 8d7af8811f9346fd059666d31a14d64ef9c5a9ca
---

## 4. Turning it on

```bash
# 1. close the existing gaps first, so the flag starts from a clean state
python manage.py reconcile_udo_mirror --apply
python manage.py repair_qr_files --apply

# 2. set EINV_MIRROR_UDO=true, restart the service

# 3. schedule both as a safety net (hourly is ample)
python manage.py reconcile_udo_mirror --apply
python manage.py repair_qr_files --apply
```

---

## 5. What to test in production

### 5.1 One real invoice, end to end

Generate an IRN for a single invoice in each company, then check all four
artefacts:

- [ ] PNG exists in the **OMS** folder, named `<irn>.png`
- [ ] PNG exists in the **SAP** folder, same name
- [ ] `OMS_IRN_LOG` has an `'S'` row whose `U_UTL_QRPT` points at the **OMS** folder
- [ ] `@UTL_MDEXTH` has an `'S'` row whose `U_UTL_QRPT` points at the **SAP** folder,
      with `Object = 'UTL_MDEXT_UOM'`, `Creator = 'OMS'`, `DocNum = DocEntry`,
      and `Period` matching the invoice's posting period

```sql
SELECT "DocEntry","DocNum","Object","Creator","Period","U_UTL_IST","U_UTL_QRPT"
  FROM "<schema>"."@UTL_MDEXTH" WHERE "U_UTL_BaseEntry" = '<invoice DocEntry>';
```

### 5.2 The bill actually prints — both ways

This is the test that matters; the rows above can all look right while the bill
is blank.

- [ ] Print from **OMS** — QR visible
- [ ] Print from **SAP** — QR visible
- [ ] For a **service** invoice, `CRYSTAL_AR_INVOICE_SERVICES` returns rows and
      `CRYSTAL_AR_INVOICE_ITEMS` returns none. That is correct, not a failure.

```sql
CALL "<schema>"."OMS_SP_GST_INVOICE"(<invoice DocEntry>);
CALL "<schema>"."CRYSTAL_AR_INVOICE_ITEMS"(<invoice DocEntry>);
```

An error 305 here means a duplicate `'S'` row slipped through — stop and read
§6 of `OMS_IRN_TO_SAP_UDO_TRANSFER.md` before generating anything else.

### 5.3 No duplicates created

- [ ] Every invoice has at most one OMS-written `'S'` row:

```sql
SELECT COUNT(*) FROM "<schema>"."@UTL_MDEXTH" M
 WHERE M."Creator" = 'OMS' AND M."U_UTL_IST" = 'S'
   AND EXISTS (SELECT 1 FROM "<schema>"."@UTL_MDEXTH" O
                WHERE O."U_UTL_BaseEntry" = M."U_UTL_BaseEntry"
                  AND O."U_UTL_DocType"  = M."U_UTL_DocType"
                  AND O."Creator" <> 'OMS' AND O."U_UTL_IST" = 'S');   -- expect 0
```

- [ ] Take an invoice the add-on has **already** e-invoiced and let OMS attempt
      it: expect no new row and a log line saying the invoice already has an
      `'S'` row. That is the guard doing its job, not an error.

### 5.4 Key allocation under load

`@UTL_MDEXTH."DocEntry"` is not an identity column and OMS competes with the
add-on for the next key. Generate several IRNs close together, ideally while SAP
is also billing, then confirm:

- [ ] `MAX("DocEntry") - COUNT(*) = 0` — the sequence is still gap-free
- [ ] no "DocEntry race" retry-exhausted errors in the log (a few retry lines are
      normal and harmless)

### 5.5 Cancellation

- [ ] Cancel an IRN, then confirm `Canceled = 'Y'` on **both** rows
- [ ] Re-print: no QR, no IRN

> Not available in every company. The cancel stamp needs `UPDATE` on the schema,
> and one company has not been granted it — there the OMS row is stamped and the
> SAP row is not, so a cancelled IRN keeps printing on SAP-side layouts. The code
> is already wired and starts working the moment the grant is in place. Until
> then, treat cancellations in that company as a manual step.

### 5.6 Failure behaviour

- [ ] Point one QR folder at an unreachable path temporarily and generate an IRN:
      the IRN must still be created and the table rows still written. Only the
      file is missing, and `repair_qr_files` must then list it and fix it with
      `--apply`.

---

## 6. Rollback

```ini
EINV_MIRROR_UDO=false
```

Restart. OMS reverts to writing only `OMS_IRN_LOG`; nothing else changes and
existing rows are left alone.

**Rows already written cannot be deleted** — the account OMS connects with has
no `DELETE` on any company schema. A bad row can only be neutralised, by setting
`U_UTL_IST` to something other than `'S'`, which removes it from every report's
filter. That needs `UPDATE`, which one company does not have (§5.5). Plan
accordingly before enabling there.

---

## 7. The three commands, and which fault each fixes

| command | fault |
|---|---|
| `reconcile_udo_mirror` | row in `OMS_IRN_LOG`, **no row** in `@UTL_MDEXTH` — prints from OMS, blank from SAP |
| `repair_qr_files` | row has a path, **file missing** from that folder — prints with an empty QR box, silently |
| `backfill_qr_png` | row has **no path** at all — report has nothing to open |

All three are idempotent, report-only without `--apply` (`--dry-run` for
`backfill_qr_png`), and safe to schedule.

Nothing is lost when a PNG goes missing: the QR is only a render of the signed
string stored on the row, so it can always be rebuilt.

---

## 8. Known limitations

1. **One company lacks `UPDATE`** — cancellations do not reach its SAP-side row
   (§5.5). Wired and dormant until granted.
2. **No company grants `DELETE`** — mistakes can be neutralised, not removed (§6).
3. **The add-on can still IRN an invoice OMS has already done.** OMS's guard only
   governs OMS's own writes. If both systems e-invoice the same document, the
   duplicate that breaks printing is created outside OMS's control. Either stop
   the add-on touching OMS-billed invoices, or rely on the scheduled checks to
   surface it.
4. **`ONNM.AutoKey` for this UDO lags `MAX("DocEntry")`** in every company. This
   predates OMS writing here and is why the add-on evidently derives its key from
   the table rather than from `ONNM`. Harmless today; it would matter if anything
   created this object through the SAP UI. See
   `OMS_IRN_TO_SAP_UDO_TRANSFER.md` §10.
<<<<<<< HEAD
=======

---

## 9. Enablement record — 2026-09-19

Run on the app host `10.10.101.118`, file server `10.10.101.52`.

### 9.1 Verified privileges — the connecting user is `B1i`, not `DSRN`

`OMS_IRN_TO_SAP_UDO_TRANSFER.md` §2 documents `DSRN`. The application actually
connects as **`B1i`**, and its grants differ per company — confirmed against
`SYS.EFFECTIVE_PRIVILEGES` with `USER_NAME = CURRENT_USER`:

| Schema | SELECT | INSERT | UPDATE | DELETE |
|---|:--:|:--:|:--:|:--:|
| `JIVO_OIL_HANADB` | ✅ | ✅ | ✅ | ❌ |
| `JIVO_BEVERAGES_HANADB` | ✅ | ✅ | **❌** | ❌ |
| `JIVO_MART_HANADB` | ✅ | ✅ | ✅ | ❌ |

Beverages is the constrained company referred to throughout §5.5, §6 and §8.1.
An `UPDATE` attempted there fails with HANA **error 258 `insufficient privilege`**
(confirmed empirically, not just from the catalogue). The fix is one grant, and it
also un-blocks IRN cancellations reaching Beverages' SAP-side rows:

```sql
GRANT UPDATE ON SCHEMA "JIVO_BEVERAGES_HANADB" TO B1I;
```

### 9.2 `.env`

`EINV_MIRROR_UDO=true` added. Everything else §2 asks you to verify was already
correct: `EINV_MIRROR_HANA=true`, the three `EINV_QR_SAVE_DIR_*` on the SAP
Bitmaps folders, `EINV_QR_ROOT` on the OMS root, `EINV_QR_FILENAME={irn}.png`,
SMB creds set, and all three company DBs resolving to live schemas.

Two traps worth knowing:

- **The company-DB keys are not named what §2 implies.** `settings.py` reads
  `HANA_DB_OIL_NAME` / `HANA_DB_BEVERAGE_NAME` / `HANA_DB_MART_NAME` — *not*
  `HANA_*_COMPANY_DB`. A stale `HANA_BEVERAGE_COMPANY_DB` in `.env` is shadowed
  and never wins. Always confirm via the `manage.py shell` one-liner in §2, never
  by grepping `.env`.
- **`EINV_QR_FILENAME` is load-bearing and its code default is wrong for this.**
  `settings.py` defaults to `{doc_no}.png`; the dual write needs `{irn}.png`,
  because the file duplicate check matches on that name and `@UTL_MDEXTH` paths
  are rebuilt as `<IRN>.png`. Never drop that line from `.env`.

### 9.3 Backfill applied

| | OIL | BEVERAGE | MART |
|---|---|---|---|
| `reconcile_udo_mirror --apply` | 6 rows → DocEntry 19043–19048 | 5 rows → DocEntry 5324–5328 | up to date (0 rows) |
| `Creator='OMS'` after | 97 → **103** | 101 → **106** | — |
| `repair_qr_files --apply` | 47 PNGs | ~75 PNGs | n/a |
| PNG coverage after | 103/103 | 112/112 | n/a |

All §5.3 invariants re-checked green in both companies: `MAX("DocEntry") − COUNT(*) = 0`,
`DocNum = DocEntry`, and zero OMS rows duplicating an add-on `'S'` row.

**The outcome that matters:** `CRYSTAL_AR_INVOICE_ITEMS` now returns the IRN, Ack
number and QR path for these invoices and agrees with `OMS_SP_GST_INVOICE` — it
read `@UTL_MDEXTH` only, so before the mirror they printed blank from every
SAP-side layout. No error 305 in either company.

### 9.4 `OMS_IRN_LOG` path normalisation

Historic rows carried up to four path forms, two of them on hosts that no longer
resolve (`\\20.20.45.25\…`, `\\JIVO-APP\…`), plus rows pointing into the **SAP**
folder — which contradicts §1, where `OMS_IRN_LOG` is supposed to record the OMS
folder. Normalised for Oil:

```sql
UPDATE "JIVO_OIL_HANADB"."OMS_IRN_LOG"
   SET "U_UTL_QRPT" = '\\10.10.101.52\OMS_Attachments\OIL_ATTACHMENTS\Bitmap\'
                      || "U_UTL_IRN" || '.png'
 WHERE "U_UTL_IST" = 'S' AND LENGTH(IFNULL("U_UTL_IRN",'')) > 0;
```

**103 rows updated.** Always list the target folder first and exclude any row whose
`<irn>.png` is absent, or the rewrite creates the silent blank-QR fault this doc
exists to prevent. One Oil row was excluded on exactly that basis (BaseEntry 80518,
a fresh IRN whose PNG had not reached the OMS folder); it keeps its SAP-folder path,
which resolves.

**Beverages is still pending** — the same statement against
`BEVERAGE_ATTACHMENTS\Bitmap` covers **112 rows, all with their PNG present**, and
fails only on the missing `UPDATE` grant in §9.1. Of those, only 16 are actually
broken (`\\20.20.45.25\…`); the rest resolve today, so this is normalisation, not
a repair.

> **`EINV_QR_SAVE_DIR` points at a folder that does not exist.** The legacy
> single-folder fallback is `\\10.10.101.52\OMS_Attachments\Bitmap`, but that share
> holds only `OIL_ATTACHMENTS\`, `BEVERAGE_ATTACHMENTS\`, `MART_ATTACHMENTS\` — there
> is no `Bitmap` directly under it. Any company falling through
> `qr_dir_for_company()` will fail its QR save. Latent, not biting today.

### 9.5 Still open

1. **Restart the OMS service.** `EINV_MIRROR_UDO=true` is not read until then, so
   new IRNs keep writing no UDO row and accruing fresh gaps — observed live: Oil
   BaseEntry **80518** and a new Beverages row both appeared *during* this work,
   minutes after `reconcile_udo_mirror` had reported "up to date".
2. **Beverages `UPDATE` grant** (§9.1) — unblocks both the path rewrite and
   cancellations.
3. **Beverages has 2 rows with no QR path at all.** `repair_qr_files` cannot fix
   these; they need `backfill_qr_png --company BEVERAGE` (note its report flag is
   `--dry-run`, not `--apply`).
4. **Mart has never been examined** for the UDO transfer and has 0 signed rows.
>>>>>>> 8d7af8811f9346fd059666d31a14d64ef9c5a9ca
