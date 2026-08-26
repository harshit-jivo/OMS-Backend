# File Upload Utility ("Advanced File Upload API")

In-house file-drop service that writes uploaded files onto the **SAP attachment
network shares** and keeps a database record of every file, uploader and event.
It is a separate project from OMS — OMS does not talk to it yet (verified: no
reference to port 8013 or `fu_api_` anywhere in this repo).

It exists because SAP Business One's Service Layer cannot upload attachments on
this installation — every attachment call returns `-5002`, because the Service
Layer host (`.222`) cannot reach the attachment share. This utility runs on the
118 server, which *can* reach it, and writes the file to the same folder the B1
Windows client uses as its attachment target, so the file is already in place
and SAP only needs the path. See `docs/ap-invoice-service-layer.md` for the
full diagnosis.

- **Base URL:** `http://138.252.101.118:8013`
- **Framework:** FastAPI on uvicorn — interactive docs at `/docs`, machine-readable
  spec at `/openapi.json`
- **Dashboard:** `/` redirects (307) to `/upload.html`; `/login.html` and
  `/index.html` also exist
- **Deployed from:** `C:\inetpub\wwwroot\file_uploder` on the 118 server

> The `curl` examples circulated internally use `http://localhost:8001`. That is
> the port the app listens on locally; from anywhere else use **8013**.

Everything below was verified against the live service on 2026-08-24. Test files
created during verification were deleted afterwards — the file count was 666
before and 666 after.

---

## 1. Authentication

Two independent mechanisms. Every endpoint except `/health` requires one of them.

### API clients — `X-API-Key`

```
X-API-Key: fu_api_...
X-Uploader: user@example.com     # optional
```

`X-Uploader` is **optional**, not required. When omitted, the uploader is
recorded as the username that owns the token (verified: a token owned by `oms`
recorded `uploader: "oms"`). Supply it whenever you know the real end user, or
the audit trail attributes every upload to the service account.

The OMS token is owned by user `oms` (id 4, role `admin`), is permanent and has
no expiry.

### Dashboard users — `Authorization: Bearer`

```http
POST /auth/login
Content-Type: application/json

{"username": "oms", "password": "Jivo@1234"}
```

```json
{"status":"success","data":{
  "access_token":"fu_session_...","token_type":"bearer",
  "expires_at":"2026-08-24T18:44:31Z",
  "user":{"id":4,"username":"oms","email":"oms@jivo.in","role":"admin","is_active":true}
}}
```

Session tokens are prefixed `fu_session_` and last **12 hours**. Send as
`Authorization: Bearer fu_session_...`. `POST /auth/logout` invalidates one;
`GET /auth/me` returns the caller's identity and is the quickest way to confirm
a credential works.

Known accounts: `oms` / `Jivo@1234`, and `admin` / `admin@123`. All four users
currently in the system hold role `admin`.

### Auth failures

| Situation | HTTP | `error.code` |
|---|---|---|
| No credential sent | 401 | `AUTH_REQUIRED` |
| Unknown, revoked or expired token | 403 | `AUTH_INVALID` |

---

## 2. Response envelope

Every JSON response is wrapped. Success:

```json
{"status": "success", "data": ...}
```

Failure:

```json
{"status": "error", "error": {"code": "FOLDER_NOT_FOUND", "message": "No folder exists for id 9999."}}
```

`/upload` adds a third state, **`partial`** — see the warning below.

Schema violations (missing field, name too long) return FastAPI's own 422 with a
`detail[]` array instead of this envelope.

---

## 3. Folders

Uploads target a **folder id**, never a raw path. Folders are pre-registered
server side, which is what keeps callers from writing anywhere on the network.

`GET /folders` — live registrations:

| id | name | path |
|---|---|---|
| 2 | test | `C:\inetpub\wwwroot\file_uploder\test` |
| 3 | Jivo Oil SAP Attachments | `\\10.10.101.52\Attachments_Oil\JIVO_OIL\Attachments` |
| 4 | Jivo Beverages SAP Attachments | `\\10.10.101.52\Attachments_Bev\JIVO_BEVERAGES\Attachments` |
| 5 | Jivo Mart SAP Attachments | `\\10.10.101.52\Attachments_Mart\JIVO_MART\Attachments` |

**Folder 3's path is exactly the B1 Windows client's `ATC1.trgtPath` for Jivo
Oil.** A file uploaded to folder 3 lands where SAP already expects attachments.
Use **folder 2 for all testing** — it is local disk, not a SAP share.

- `POST /folders` — `{"name": "...", "path": "..."}` (name ≤120, path ≤1000)
- `PUT /folders/{id}` — same body, both fields required
- `DELETE /folders/{id}`
- `POST /folders/test` — `{"path": "..."}`, probes a path without registering it

`POST /folders/test` always returns 200; read the booleans:

```json
{"status":"success","data":{
  "path":"\\\\10.10.101.52\\SAP Attachments\\Jivo Oil\\Bitmaps",
  "exists":true,"is_directory":true,"parent_exists":true,"writable":true}}
```

This is a genuinely useful diagnostic: it reports reachability and writability
of any UNC path **as seen from the 118 server**, which is hard to determine
otherwise. It confirmed the e-invoice QR folder
`\\10.10.101.52\SAP Attachments\Jivo Oil\Bitmaps` exists and is writable.

Note that this makes any valid API key able to probe the server's filesystem and
network shares. Low severity, but it is an information-disclosure surface.

---

## 4. Upload

```bash
curl -X POST http://138.252.101.118:8013/upload \
  -H "X-API-Key: $KEY" \
  -H "X-Uploader: user@example.com" \
  -F "folder_id=3" \
  -F "files=@report.pdf" \
  -F "files=@photo.jpg"
```

`multipart/form-data`; `folder_id` (integer) and at least one `files` part are
both required. Repeat `files` for multiple uploads.

```json
{"status":"success","data":{
  "files":[{"id":691,"original_name":"doc probe (1).txt","stored_name":"doc probe _1_.txt",
            "size":14,"content_type":"text/plain","folder_id":2,
            "uploaded_at":"2026-08-24T06:44:52Z","uploader":"user@example.com"}],
  "failures":[]}}
```

### ⚠️ HTTP 200 does not mean every file was stored

When some files succeed and others fail, the status is **`partial`** and the
response is still **HTTP 200**:

```json
{"status":"partial","data":{
  "files":[{"id":694,"stored_name":"b_v2.txt", ...}],
  "failures":[{"file":"bad.exe","code":"FILE_TYPE_NOT_ALLOWED",
               "message":"This file extension is not allowed."}]}}
```

Any integration **must** inspect `data.failures` rather than trusting the status
code. Checking only `response.ok` will silently lose files.

### Filename handling

The stored name is not the name you sent — plan for this.

- Unsafe characters are replaced with `_`:
  `doc probe (1).txt` → `doc probe _1_.txt`. Spaces survive; parentheses do not.
- With `version_duplicates` on (current setting), a colliding name gets a
  version suffix instead of overwriting: `b.txt` → `b_v2.txt` → `b_v3.txt`.

Always persist the returned `id` and `stored_name`. Reconstructing the path from
the name you uploaded will be wrong.

### Upload errors

| Cause | HTTP | Where it surfaces |
|---|---|---|
| Extension not in the allow-list | 200 | `failures[].FILE_TYPE_NOT_ALLOWED` |
| `folder_id` does not exist | 404 | `error.FOLDER_NOT_FOUND` |
| No `folder_id` / no `files` | 422 | `detail[]` |

Current limits (`GET /settings`): **100 MB** per file; allowed extensions
`.csv .doc .docx .gif .jpeg .jpg .pdf .png .txt .xls .xlsx .zip`.
A 35 MB upload was verified to succeed, so no reverse-proxy request cap is
truncating the configured limit below that.

---

## 5. Files

| Endpoint | Notes |
|---|---|
| `GET /files` | All file metadata |
| `GET /files/{id}/preview` | `Content-Disposition: inline` |
| `GET /files/{id}/download` | `Content-Disposition: attachment` |
| `PATCH /files/{id}` | `{"filename": "new-name.pdf"}` (1–220 chars) |
| `DELETE /files/{id}` | Hard delete |

A metadata record:

```json
{"id":690,"folder_id":3,"original_name":"logo.png","stored_name":"logo.png",
 "path":"\\\\10.10.101.52\\Attachments_Oil\\JIVO_OIL\\Attachments\\logo.png",
 "content_type":"image/png","size":7722,"uploader":"admin",
 "uploaded_at":"2026-08-22T11:32:46Z","folder_name":"Jivo Oil SAP Attachments"}
```

`path` is the absolute location on disk — that is the value to hand to SAP.

`GET /files` takes **no query parameters**: no filtering, no pagination, no
sorting. It returns every record in one response (666 at time of writing) and
grows without bound. Filter client side, and expect this endpoint to get slower
over time. If OMS ends up polling it, add server-side filtering by `folder_id`
first.

Both preview and download send `ETag`, `Last-Modified` and `Accept-Ranges:
bytes`, so conditional and range requests work.

Deleting a missing id returns 404 `FILE_NOT_FOUND`. Delete is a hard delete —
the row and the file both go, with no restore path. There is no trash.

### ⚠️ Rename bypasses the extension allow-list

`/upload` rejects `bad.exe` with `FILE_TYPE_NOT_ALLOWED`, but
`PATCH /files/{id}` with `{"filename": "evil.exe"}` **succeeds** and the file is
renamed to `evil.exe` on the share. Verified on the live service.

So the allow-list is an upload-time check only, not an invariant. Anyone with an
API key can place an arbitrarily-named executable on a SAP attachment share in
two calls. Whether that is exploitable depends on what else reads those shares,
but the validation belongs in the rename path too. Worth fixing in the utility.

---

## 6. Admin endpoints

Available to dashboard sessions and, in practice, to the OMS API key as well —
it belongs to an `admin` user, so all of these answered successfully with just
`X-API-Key`. Role separation is defined (`^(admin|user)$`) but untested: every
existing user is an admin, so whether a `user`-role credential is actually
restricted here has not been confirmed.

### Tokens

- `GET /tokens?scope=all` — `scope` defaults to `mine`; `all` for every token
- `POST /tokens` — `{"name", "user_id"?, "is_permanent"?: false, "expires_in_minutes"?: 1440}`
  (max 525600 = 1 year; `is_permanent: true` for no expiry)
- `POST /tokens/{id}/revoke`, `POST /tokens/{id}/regenerate`
- `GET /token-logs?token_id={id}` — `create` / `login` / `revoke` events

Tokens are listed as a masked prefix (`fu_api_Wy3IL...Nhvs`) with `status` of
`active` or `revoked`. The full secret is shown only once, at creation — capture
it then. `last_used_at` is maintained, which makes stale tokens easy to spot.

### Users

- `GET /users` — includes `token_count`
- `POST /users` — `{"username" (3–48), "password" (≥6), "email"?, "role"?: "user"}`
- `PATCH /users/{id}` — any of `email`, `role`, `is_active`, `password`
- `DELETE /users/{id}` — **disables** the user (`is_active`), does not erase them

### Settings

`GET /settings` / `PUT /settings`:

```json
{"allowed_extensions": [".pdf", ".jpg"], "max_file_size_mb": 100,
 "version_duplicates": true, "upload_logging": true}
```

`PUT` requires `allowed_extensions` (≥1 entry) and `max_file_size_mb`
(1–102400). It is a **full replace, not a merge** — send the complete extension
list every time or you will silently drop the missing ones.

Turning off `version_duplicates` makes same-name uploads overwrite instead of
versioning. Turning off `upload_logging` stops `/logs` from recording uploads.

### Logs and health

- `GET /logs` — file events (`upload`, `delete`) with `file_id` and `uploader`
- `GET /health` — `{"status":"ok"}`, the only unauthenticated endpoint; use for monitoring

---

## 7. Using this from OMS

The natural fit is the AP invoice attachment problem. The Service Layer route
fails with `-5002` because the SL host cannot reach the attachment share, and
the only AP invoices that post successfully do so by reusing the GRPO's existing
`AttachmentEntry`. This utility sidesteps that: upload the file to the company's
folder, then give SAP the returned `path`.

Sketch for a Django-side helper:

```python
import requests
from django.conf import settings

FOLDER_IDS = {"OIL": 3, "BEVERAGE": 4, "MART": 5}

def upload_attachment(company, django_file, uploader_email):
    resp = requests.post(
        f"{settings.FILE_UPLOAD_URL}/upload",
        headers={
            "X-API-Key": settings.FILE_UPLOAD_TOKEN,
            "X-Uploader": uploader_email,
        },
        data={"folder_id": FOLDER_IDS[company]},
        files={"files": (django_file.name, django_file, django_file.content_type)},
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()

    # HTTP 200 can still mean the file was rejected — status may be "partial".
    if body.get("status") == "error":
        raise RuntimeError(body["error"]["message"])
    failures = body["data"]["failures"]
    if failures:
        raise RuntimeError(failures[0]["message"])

    return body["data"]["files"][0]      # keep id + stored_name, not your own name
```

Config to add to `.env` (do not hardcode the token):

```
FILE_UPLOAD_URL=http://138.252.101.118:8013
FILE_UPLOAD_TOKEN=fu_api_...
```

Points to settle before wiring it in:

- **Store `id` and `stored_name`** on the OMS-side row. The name is rewritten on
  upload and the path cannot be reconstructed from the original name.
- **Validate before sending.** The allow-list and 100 MB cap are enforced by the
  utility, but a client-side check gives the user a real error message instead of
  a `failures[]` entry to unpack.
- **Deletes are not coordinated.** Deleting an OMS row leaves the file on the
  share unless you also call `DELETE /files/{id}`, and the utility's delete is
  irreversible. Given OMS's tracker uses soft deletes, decide deliberately
  whether a soft delete should hard-delete the file.
- **Plaintext HTTP.** The token travels unencrypted over the LAN. Acceptable
  internally; worth noting if this is ever reached from outside.

---

## 8. Endpoint index

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness (no auth) |
| POST | `/auth/login` | Dashboard login → `fu_session_` token |
| POST | `/auth/logout` | Invalidate session |
| GET | `/auth/me` | Current identity |
| POST | `/upload` | Upload one or more files |
| GET | `/files` | All file metadata (no filters) |
| GET | `/files/{id}/preview` | Inline stream |
| GET | `/files/{id}/download` | Attachment stream |
| PATCH | `/files/{id}` | Rename |
| DELETE | `/files/{id}` | Hard delete |
| GET | `/folders` | List folders |
| POST | `/folders` | Register folder |
| PUT | `/folders/{id}` | Update folder |
| DELETE | `/folders/{id}` | Remove folder |
| POST | `/folders/test` | Probe a path (exists / writable) |
| GET | `/settings` | Current limits |
| PUT | `/settings` | Replace limits |
| GET | `/logs` | File event log |
| GET | `/users` | List users |
| POST | `/users` | Create user |
| PATCH | `/users/{id}` | Update user |
| DELETE | `/users/{id}` | Disable user |
| GET | `/tokens` | List tokens (`?scope=mine\|all`) |
| POST | `/tokens` | Create token (secret shown once) |
| POST | `/tokens/{id}/revoke` | Revoke |
| POST | `/tokens/{id}/regenerate` | Rotate |
| GET | `/token-logs` | Token audit (`?token_id=`) |

`/health`, `/auth/logout`, `/auth/me` and `/logs` are live but were missing from
the internal endpoint list.

---

## 9. Open items

1. **Rename bypasses the extension allow-list** (§5) — validation should be
   applied on rename as well as upload.
2. **`GET /files` has no filtering or pagination** (§5) — add `folder_id` at
   least before OMS depends on it.
3. **Role enforcement unverified** — no non-admin user exists to test whether
   `user` role is actually restricted from `/users`, `/tokens` and `/settings`.
4. **`admin` / `admin@123`** is a live admin account with a guessable password on
   a service that writes to SAP shares. Worth changing.
5. **No OMS integration yet** — §7 is a proposal, not shipped code.
