# OMS → Jivo Auth integration plan

Status, 2026-10-01: phases 0–5 done locally (switched and verified, nothing committed). Phase 6 (cleanup) is **prepared, not applied**: [jivo-auth-cleanup/](jivo-auth-cleanup/README.md). It waits until every OMS on the shared .117 DB, including the old-login server on 10.10.101.118, and the mobile app use Jivo sign-in.
Written 2026-10-01 against the `test` branch (`fb2aa38`) of OMS-Backend and OMS-Frontend.

OMS stops issuing its own tokens and checking passwords. Users sign in at
Jivo Auth (`https://auth.jivo.in`), and OMS accepts the Jivo access token.
Every OMS user row keeps its primary key, every FK keeps pointing at it, and
roles, `extra_pages`, assignments and staff flags stay in OMS.

## 1. Prerequisites and decisions

### Environment for this work

| | |
|---|---|
| OMS backend | local, `runserver 127.0.0.1:8001`, no Docker |
| OMS frontend | local, Vite on `http://localhost:5173` |
| OMS database | `order_management` on 138.252.101.117 (shared **test** DB, confirmed by the owner) |
| Jivo Auth | **production**, `https://auth.jivo.in` |
| Staging / production OMS | not part of this work |

### Jivo Auth

| # | Item | Status |
|---|---|---|
| 1 | Auth URL `https://auth.jivo.in` | ✅ `/api/v1/health/` → `{"status":"ok"}` |
| 2 | API base `https://auth.jivo.in/api/v1` | ✅ |
| 3 | JWKS | ✅ one RS256 key |
| 4 | Issuer | = URL (checked when the first token is decoded) |
| 5 | `import_users` on the Auth host | ⚠️ Likely: the production OpenAPI schema is identical to commit `98cfb48`, which ships it. **Confirm on the host** (`manage.py help import_users`). |
| 6 | Client package commit | ✅ `98cfb48d986ceb40269fe969eb04ff7fbe6b7b87` (main), `jivo-auth-client` 0.3.0 |
| 7 | Jivo Auth admin account | **Owner: the person running the integration.** Used in the admin by that person only, never by scripts. |
| 8 | Application slug | Proposed **`oms`**. Must be created in Admin → Applications if it doesn't exist. Never changed later. |
| 9 | API key | **Owner: admin (item 7).** Created with the application and pasted straight into `backend/.env` as `JIVO_AUTH_API_KEY`. Never in chat or git. |
| 10 | Running `import_users` | **Open:** who runs it on the Auth host, and how. The documented path is `docker compose exec -T auth python manage.py import_users oms - …` on that host. "No Docker" applies to this OMS workspace; using the Auth host's existing container is the host owner's call. |
| 11 | Auth DB credentials | Not used. |
| 12 | CORS for `http://localhost:5173` | ❌ **Not allowed yet**: the preflight returns 200 with no `access-control-allow-origin`. The admin must add it to `CORS_ALLOWED_ORIGINS` on the Auth server. Also needed: `https://oms.jivo.in` before any real deployment. |
| 13 | Test accounts | **Owner: the person running the integration.** One Jivo account granted `oms`, one not. Passwords never in chat or repo. |
| 14 | Email (SMTP) at Jivo Auth | **Open:** is SMTP configured in production? Without it, forgot-password doesn't work and admins set passwords. |

### Decisions

| # | Decision | Answer |
|---|---|---|
| D1 | Keep existing passwords | **Yes.** All 112 hashes are `pbkdf2_sha256`, which Jivo Auth verifies, so no one needs a reset. |
| D2 | Mark emails verified (`--mark-verified`) | **Yes.** Caveat: of the 72 active users with email, 45 are `@gmail.com`, 11 are placeholder `@oms.com` addresses that aren't real mailboxes, 1 is `admin@example.com`, and 15 are `@jivo.in`. Fine for a test; revisit before production data. |
| D3 | Link to existing Jivo accounts by email | **Yes, reviewed in the dry run.** Some `@jivo.in` users (e.g. `naresh@jivo.in`, `developer@jivo.in`) probably already have Jivo accounts. Each `linked` row is checked to be the same person. |
| D4 | `LOCAL_USER_LINK_BY_EMAIL` | **On**, following D2. |
| D5 | Who is migrated | **Active users with email: 72.** The 29 inactive users keep their rows and data but get no Jivo account. |
| D6 | No email or duplicates | No duplicate emails exist (case-insensitive). 11 active users have no email and stay unmapped: `b1_auth_probe`, `dash-granted`, `dash-worker`, `dash-perm-granted`, `dash-perm-worker`, `dash-perm-nobody`, `dash-perm-admin` (superuser), `Divjot`, `Keshav`, `Manroop`, `Accounts`. Add an email and re-run to migrate any of them. |
| D7 | Break-glass local superuser | **Proposed: none** for this test. Can be added later from `templates/break_glass_backend.py`. |
| D8 | Who creates users | Accounts are created in the Jivo Auth admin and granted `oms`. OMS's own "create user" screens become "add a Jivo user to OMS" (pick by email or ID, then set OMS role and assignments). The `oms` API key can only *list* the app's users (`GET /api/v1/apps/users/`), not create them. |
| — | Advance-payment password re-confirmation | **Fresh Jivo sign-in.** The frontend re-authenticates at Jivo Auth. OMS accepts the new access token only if its `sub` equals the user's `auth_id` and it was issued within the last 2 minutes, then issues the same signed 30-minute token as today. |

### OMS environment variables (added to `.env.example` without values)

`JIVO_AUTH_URL=https://auth.jivo.in`, `JIVO_AUTH_APP=oms`, `JIVO_AUTH_API_KEY` (secret), `JIVO_AUTH_NUM_PROXIES` (0 locally, 1 behind Nginx).
OMS already loads `.env` through `python-decouple` (`OMS/settings.py`). The `JIVO_AUTH` dict passes the values in explicitly, because the client package reads `os.environ`, which decouple doesn't populate.

## 2. The user model

`users.User(AbstractUser)` (`users/models.py:239`), table `users_user`, `AUTH_USER_MODEL='users.User'`.

- PK `id`: integer. Stays.
- `USERNAME_FIELD='username'` (unique, case-sensitive; `admin`/`Admin` and `Parmeet`/`parmeet` both exist). `EMAIL_FIELD='email'`, which is nullable, **not unique** and `max_length=150`. `first_name`/`last_name` are removed; there is a single `name` (NOT NULL).
- New Jivo-created rows get `name=''`, `role=NULL` and no assignments, which the DB accepts. An admin then sets the role, or `sync_jivo_users` fills in `name`.

**Row counts on .117 (2026-10-01):** 112 total · 83 active · 29 inactive · 11 active without email · 0 duplicate emails · 5 `is_staff` · 6 `is_superuser` · 0 unusable passwords.

### Field classification

| Moves to Jivo Auth (OMS keeps a read-only copy) | Stays in OMS, unchanged | Authentication-only (cleanup) |
|---|---|---|
| `email` (rewritten to Jivo's lowercased form on first token) · `name` (exported as `first_name`, refreshed by `sync_jivo_users`) | `id` · **`auth_id` (new)** · `username` (no longer a credential; `invoiceWarehouses.ts` keys on `kp`/`preshit`) · `phone` · `role` · `extra_roles` · `company` · `main_group`/`main_groups` · `state`/`states` · `category`/`categories` · `sub_group` · `extra_pages` · `is_active` (now "blocked in OMS") · `is_staff` · `is_superuser` · `groups` · `user_permissions` · `date_joined` · `last_login` · `created_at`/`updated_at` · `created_by`/`updated_by` | `password`: kept as a column, made unusable at cleanup |

Export mapping for the templates: `email→email`, `first_name→name`, `last_name→None`, `employee_code→None`.

## 3. Authentication-only models and tables (removed at cleanup)

- `rest_framework_simplejwt.token_blacklist` (`OMS/settings.py:121`): tables `token_blacklist_outstandingtoken` (FK `user_id`) and `token_blacklist_blacklistedtoken`. Nothing business-related points at them.
- `SIMPLE_JWT` settings (`OMS/settings.py:623-652`).
- No OTP, MFA, lockout or verification models exist. DRF `authtoken` is not installed.

## 4. User relationships (all preserved)

**66 FK columns** (64 declared fields plus 2 inherited from abstract bases) across 15 apps. All are `ForeignKey`, no O2O. None changes, because the rows they point at don't change.

- advance_payment (11), attachments (1), audit (1), backdate (3), core abstract `created_by` → PaymentReceipt/BankDeposit (2), devices (1), invoice (4), legal (1), notifications (1), orders (16), payments (2 + 2 inherited `current_user`), production (2), tracker (11), users (6, incl. `User.created_by/updated_by` and the unmanaged `users_user_states`), workflow (3, PROTECT).
- User-side M2M tables: `users_user_groups`, `users_user_user_permissions`, `extra_roles`, `categories`, `main_groups`, `states`. Also `django_admin_log.user_id`.
- **Stored as text, not FK:** `audit.AuditLog.username`, `payments.PaymentStatusHistory.changed_by_username`, `invoice.InvocieHistory.created_by`, `HAIS.AssetLog.created_by`, `sap_sync.SyncLog.triggered_by` (all usernames, which stay stable), `tracker.AlertNotification.email` (a send-time snapshot).
- **User PK outside the DB:** `advance_payment/services/flow.py:537,554` (cache key and signed token, both local PK, so unaffected). The old JWT `user_id` claim goes away with the old tokens.

## 5. Change list by file

### Phase 2 — Prepare (no behaviour change)

| File | Change |
|---|---|
| `requirements.txt` | add `jivo-auth-client @ git+https://github.com/Nareshkumar124/jivo-auth.git@98cfb48d986ceb40269fe969eb04ff7fbe6b7b87#subdirectory=packages/jivo-auth-client` |
| `OMS/settings.py` | `"jivo_auth"` in `INSTALLED_APPS`. `JIVO_AUTH = {URL, APP, API_KEY, NUM_PROXIES}` read via `config()`, plus `LOCAL_USER_ID_FIELD="auth_id"` and `LOCAL_USER_LINK_BY_EMAIL=True` (set now because `sync_jivo_users --create-missing` uses them; otherwise it would key new rows on `username`). `DEFAULT_AUTHENTICATION_CLASSES` unchanged. |
| `users/models.py` | `auth_id = UUIDField(null=True, blank=True, unique=True, editable=False)` |
| `users/migrations/0038_user_auth_id.py` | new; adds the nullable column and unique index only |
| `users/management/commands/` | `export_jivo_users.py`, `link_jivo_users.py`, `sync_jivo_users.py` from the templates. Export: `first_name←name`, no `last_name`. Sync: `name←"first last"` via `full_name()`, never blanking a local name. Link: unchanged. |
| `users/tests_jivo_commands.py` | 23 tests. Export: skips no-email users, sends no staff flags, file mode 600, refuses inside the repo or an overwrite. Link: refuses an email mismatch, an `auth_id` used by someone else, a relink, or an import dry-run file; dry run and re-run are harmless. Sync: name join; app fields untouched. |
| `.env.example` | the four `JIVO_AUTH_*` keys, no values |

### Phase 4 — Switch

**Backend: remove**
- `users/urls.py` `login/`, `refresh/`, `logout/`, plus `LoginView`, `AuthTokenRefreshView`, `ActiveUserTokenRefreshSerializer` and `LogoutView` (`users/views/auth.py`). `login/` answers **410 Gone** with "sign in at auth.jivo.in", because the separate React Native OMS-app can't update on the same day.
- `LoginSerializer` (`users/serializers.py:216`).
- `SIMPLE_JWT` (the `token_blacklist` app stays until cleanup).

**Backend: rewrite**
- `OMS/settings.py`: `DEFAULT_AUTHENTICATION_CLASSES = [jivo_auth.authentication.JivoJWTAuthentication]`. `JIVO_AUTH` gets `LOCAL_USERS=True`, `LOCAL_USER_ID_FIELD="auth_id"`, `LOCAL_USER_LINK_BY_EMAIL=True`. `AUTHENTICATION_BACKENDS = ["jivo_auth.backends.JivoAuthBackend"]` (no `ModelBackend`). Add `jivo_auth.middleware.JivoSessionMiddleware` right after `AuthenticationMiddleware`. `CACHES` gets a shared DB cache if several processes run.
- `audit/middleware.py:26-38`: decodes with SimpleJWT's `JWTAuthentication` directly → use `JivoJWTAuthentication`.
- `users/serializers.py`: `CreateUserSerializer` → "add Jivo user" (input `auth_id` or email, looked up with `AuthClient().get_users()`, then `add_jivo_user()` from switch.md, then the same OMS fields as today, minus `password`/`username`). `UpdateUserSerializer`: drop `password`, make `email` and `name` read-only. `UserSerializer`: expose `auth_id` read-only.
- `users/views/accounts.py` `CreateUserView` follows the serializer. The other user views are unchanged.
- `users/admin.py`: base the admin on `ModelAdmin` (closes `<id>/password/`). Drop `password`/`add_fieldsets`. `auth_id` read-only in `list_display` with a "Linked to Jivo" filter. `email`/`name` read-only. `has_add_permission=False` with a note. `admin.site.login_form` relabels the username box as "Email".
- `tracker/admin_views.py:208-296`: tracker-user create/PATCH no longer sets passwords; create becomes "add Jivo user" with a tracker role.
- `advance_payment/services/flow.py:540-554` and `advance_payment/views.py:954-969`: `confirm-password` takes `{access}` (a fresh Jivo token) instead of `{password}`. It verifies the token with the client package and checks `sub == user.auth_id` and `iat` within 120 s. The 5-failures-per-15-minutes limit and the 30-minute signed token stay.
- `users/management/commands/create_super_user.py`: no password. Takes `--auth-id`/`--email` of an existing Jivo user and sets the admin role and pages.
- `core/tests.py` `PUBLIC_ROUTES`: drop `/api/auth/refresh/`; `/api/auth/login/` stays public as the 410 stub.
- `Dockerfile:168` HEALTHCHECK curls `/api/auth/login/`. It keeps working against the 410 stub, but should point at `core/health.py` instead.

**Backend: keep**
`core/permissions.py` (`is_admin`, `IsAdminRole`), every `permission_classes`, role checks, `extra_pages`, `PagePermissionsView`, `ProfileView` (`/auth/profile/` is how the frontend gets the OMS user ID, roles and pages), throttles (`UserRateThrottle` keys on the local PK, which is unchanged), `devices`, and the AllowAny invoice views (they still read the token optionally).

**Backend tests**
134 `force_authenticate` uses keep working. Add tests that:
- a token for a linked user resolves to the same PK and the same business records;
- a token without `oms` gets 403;
- a locally inactive user gets 401;
- first sign-in links by email, or creates a new row;
- `login/` returns 410 and `refresh/`/`logout/` return 404;
- an old local password no longer opens `/admin/`, and `<id>/password/` is a 404;
- `confirm-password` with a stale or foreign token is refused.

Tokens come from `templates/jivo_test_tokens.py`; no test calls auth.jivo.in.

**Frontend**
- `src/services/authService.ts`, `src/pages/Login.tsx`: `POST https://auth.jivo.in/api/v1/auth/login/` with `{email, password, device_name: "oms web"}`. The field label becomes Email. Handle `email_not_verified`, 429 and 401. After sign-in, call `GET /auth/profile/` (OMS) for the OMS user and run the existing `sessionFromApi`/`saveSession`.
- `src/services/api.ts`: `doRefresh` → `POST {AUTH}/auth/refresh/`. The response is already top-level `{access, refresh}`, the shape `doRefresh` expects. Keep the Web Locks single-flight. Bare axios to the Auth origin, so no `X-*` device headers are sent and the CORS preflight stays simple. A 403 from OMS means "no access to OMS" and must not trigger a refresh.
- `src/components/Sidebar.tsx` logout: `POST {AUTH}/auth/logout/ {refresh}` (no Bearer), then clear storage. Unify the three key-clearing lists (`api.ts:32-45`, `Sidebar.tsx:148-161`, `session.ts`) so `extra_roles`, `is_superuser`, `is_staff` and `categories` are cleared too.
- `src/pages/App_User.tsx`, `src/services/userService.ts`: no password or username on create. Create picks a Jivo user (by email) and sets OMS fields. Edit has no "Change password". Email and name are read-only.
- `src/pages/Tracker_Admin.tsx` UsersTab, `src/services/trackerService.ts`: same treatment.
- `src/pages/advancePayments/ManualAccountPassword.tsx`, `src/services/advancePaymentService.ts`: the dialog signs in to Jivo Auth with `{email, password}` and sends the new `access` to `confirm-password`. The new session is then logged out.
- New `VITE_AUTH_BASE_URL` (default `https://auth.jivo.in/api/v1`). Login gets a "Forgot password?" link to `https://auth.jivo.in/forgot-password/` if SMTP is on (item 14).
- Tests: `src/auth/session.test.ts`, `src/services/api.test.ts:109-135`, `e2e/smoke-e2e-path.spec.ts`, `e2e/harness.ts`.

**Not in this repo:** the React Native **OMS-app** (separate repo) signs in at `/api/auth/login/`. After the switch it gets a 410 until it adopts the same Jivo sign-in. It needs its own change, owned by the mobile team.

### Phase 6 — Cleanup (later, separate release)

Remove `rest_framework_simplejwt.token_blacklist` and drop its two tables. Make every `password` unusable. Remove the 410 stub once the mobile app has moved.

## 6. Release plan

Everything below runs **locally against .117**. No staging or production OMS deploy is part of this work.

1. **Prepare** (phase 2): code changes, then `pg_dump -Fc` of .117 (mode 600, outside the repo), then ⛔ `migrate users 0038` on .117. That adds one nullable column; it's shared test data, and other clients that use .117 are unaffected because Django selects named columns.
2. **Migrate users** (phase 3):
   1. Export 72 users to a mode-600 file outside the repo.
   2. ⛔ Dry-run `import_users oms` on the Auth host (item 10) and review every `linked` row.
   3. ⛔ Real import.
   4. ⛔ Fresh `pg_dump`, then `link_jivo_users` dry-run, then for real on .117.
   5. Verify the counts.
   6. Delete `users.json` everywhere.
3. **Switch** (phase 4): backend and frontend together, after re-running phase 3 for anyone added meanwhile. Users sign in with **email** instead of username.
4. **Verify** (phase 5) with the test accounts (item 13).
5. **Cleanup** (phase 6): later, separately, ⛔.

Commits stay local; nothing is pushed to `harshit-jivo/OMS-*` without confirmation.

## 7. Rollback

- **Phase 2:** revert the code. `auth_id` is an unused nullable column. To remove it, migrate `users` back to `0037`.
- **Phase 3:** set `auth_id` back to NULL, or restore the pre-link dump. Jivo accounts created by the import stay in Jivo Auth. They grant nothing once `oms` is no longer accepted, and an admin can remove the grants.
- **Phase 4:** check out the previous backend and frontend commits. Password hashes, SimpleJWT and the blacklist tables are all still there.
- **After cleanup:** only the pre-cleanup dump restores it.

## 8. Risks and open questions

1. **Real accounts on production Jivo Auth from test data.** The import creates about 72 accounts on the central service (or links existing ones) and grants them `oms`. With `--mark-verified`, gmail and placeholder `@oms.com` addresses become sign-in identities. Confirm this is wanted for a test.
2. **Item 10:** who runs `import_users` on the Auth host, and whether `docker compose exec` there is acceptable.
3. **Item 12:** `http://localhost:5173` must be added to production CORS.
4. **Item 14:** is SMTP configured in production?
5. **.117 is shared.** Adding `auth_id` and populating it is visible to anyone else using .117. Harmless to old code, but worth telling its other users.
6. **Sign-in identity changes from username to email.** The 11 users without email can't sign in after the switch.
7. **Mobile app** (separate repo) breaks at the switch until updated (410 message).
8. **`name` vs first/last name.** Export sends `name` as `first_name`. `sync_jivo_users` writes `"first last"` back to `name`, so accounts linked by email (D3) keep their surname in OMS.
9. **Existing holes kept, not introduced.** No self-service endpoint lets users edit their own role or permissions (`/auth/profile/` is read-only, and user edits are `IsAdminRole`). None found.

## Migration log

**2026-10-01, .117 test DB → production auth.jivo.in, slug `oms`**

1. Took a backup with `pg_dump -Fc` (mode 600, outside the repo). Recorded exact row counts of all 147 tables.
2. Ran `migrate users 0038` on .117. It added `auth_id uuid NULL UNIQUE`. The only row-count change was +1 in `django_migrations`. OMS's own post-migrate hook re-registered the PAYMENTS/BKDT/PRDO workflow modules, which is idempotent `update_or_create`, with no row-count change.
3. `export_jivo_users`: 112 users without `auth_id` → 72 exported + 11 without email + 29 inactive. No shared emails. All 72 hashes are `pbkdf2_sha256`.
4. `super@oms.com` (pk 124, superuser) still had `Super@1234`, the default committed in `create_super_user.py`. By decision, it was imported **without a password**, so that value never became a Jivo credential. A Jivo Auth admin must set its password in the admin.
5. Ran `import_users oms - --mark-verified` on the Auth host over SSH stdin (`docker compose exec -T auth`, existing container). The export file was never written on the host.
   - Dry run: 72 created / 0 linked / 0 skipped.
   - Real run: the same. 71 notes say "password kept", 1 says "no usable password".
   - The `oms` API key lists 72 users, and their IDs and emails match the mapping.
6. Took a second backup. Ran `link_jivo_users` as a dry run, then for real: 72 linked, 0 conflicts.
7. Verified:
   - 72 linked + 11 without email + 29 inactive = 112.
   - All 147 table row counts are unchanged.
   - Every `users_user` column except `auth_id` is byte-identical to the pre-link backup for all 112 rows.
8. Shredded `users.json` and `users-import.json`. `mapping.json` (emails and Jivo IDs, no hashes) is kept in `oms_auth_test/db/jivo-migration/` as the record.

Still unmapped (active, no email): pks 125, 136–139, 351, 352, 358–361.

## Switch log (phase 4)

**2026-10-01, local only** (backend :8001 on .117, frontend :5173 → production auth.jivo.in). Nothing is committed or deployed.

Prerequisites at the switch:
- **CORS:** `http://localhost:5173` is in `CORS_ALLOWED_ORIGINS` on the Auth server, and the container was recreated with it. The preflight for login, refresh and logout allows that origin and refuses others. Allowed headers: accept, authorization, content-type, user-agent, x-csrftoken, x-requested-with. So the frontend calls Jivo Auth without OMS's `X-*` headers.
- **Item 14 (email):** production has `EMAIL_BACKEND=smtp` but no `EMAIL_HOST`, so email is effectively off. There's no "Forgot password?" link, and administrators set passwords in the Jivo Auth admin.
- **Tokens:** access tokens last 15 min and refresh tokens 30 days. Self-registration is off and verified email is required.

Backend changes:
- **Settings:** `DEFAULT_AUTHENTICATION_CLASSES = [JivoJWTAuthentication]`. `JIVO_AUTH.LOCAL_USERS = True`. `AUTHENTICATION_BACKENDS = [JivoAuthBackend]`, with no `ModelBackend`. `JivoSessionMiddleware` sits after `AuthenticationMiddleware`. `SIMPLE_JWT` is removed; the `token_blacklist` app stays until cleanup.
- **`users/views/auth.py`:** `LoginView`, the refresh view and `LogoutView` are removed. `/auth/login/` is now `JivoLoginGoneView`, which answers 410 with `sign_in_url`. `refresh/` and `logout/` return 404.
- **New `users/jivo.py`:** `jivo_directory()`, `jivo_user()`, `assert_not_in_oms()` and `add_jivo_user()`. `JivoUnavailable` gives a 503.
- **New endpoints:** `GET /api/auth/jivo-users/` (admin) and `GET /api/tracker/admin/jivo-users/` (tracker admin).
- **Creating users:** `POST /api/auth/users/create/` takes `{auth_id, …OMS fields}` (`AddJivoUserSerializer`). It refuses someone without OMS access, someone already in OMS, or an unlinked row with the same email. Tracker-user create takes `{auth_id, role, phone}`.
- **Editing users:** `PUT /api/auth/users/<id>/` and tracker-user `PATCH` ignore `password`, `name` and `email`. `UserSerializer` exposes `auth_id`.
- **Advance payment `confirm-password`:** the request body is `{access}`, from a fresh Jivo sign-in under the device name "OMS payment confirmation".
  - OMS verifies the token, `sub == auth_id` and the `oms` grant.
  - It asks Jivo Auth for that user's sessions and requires an active one under that device name created in the last 120 s. A refresh never creates a session, so a stolen refresh token can't pass.
  - It then revokes that session, so each sign-in confirms once.
- **Admin:** based on `ModelAdmin`, so there is no `<id>/password/`. `auth_id`, `name` and `email` are read-only, with a "linked to Jivo" filter. No add form. The sign-in box is labelled Email.
- **`audit/middleware.py`:** resolves the actor with `JivoJWTAuthentication`.
- **`create_super_user --email`:** promotes an existing Jivo user, with no password.
- **Dockerfile:** the HEALTHCHECK uses `/api/health/live/`.
- **Docs and config:** README auth section updated. `.env.example` no longer documents `JWT_SIGNING_KEY`.
- **Cache:** none is configured, so each process has its own memory cache. That's fine for `runserver`. A multi-worker deployment needs a shared cache: `JivoSessionMiddleware` refreshes through it, and the advance-payment failure counter lives there.

Tests:
- Full suite: 2117 tests. The 412 failures are the same set as the untouched `fb2aa38`, so none are new.
- New `users/tests_jivo_auth.py` (29 tests):
  - same PK and business records via a token;
  - 403 without the `oms` grant;
  - 401 for expired, foreign-key, refresh-type and locally inactive tokens;
  - link by email, create a row, or never claim a linked row;
  - the admin refuses an old password, can't set passwords, and has no add form;
  - the add-Jivo-user, directory and tracker flows, and a 503 when Jivo Auth is down.
- Advance-payment confirmation: 13 tests.
- Old login, refresh, throttle and password-policy tests were rewritten as 410/404 and "Jivo owns these fields" tests.

## Verification log (phase 5)

**2026-10-01**, with `super@oms.com` (pk 124), whose password was set in the Jivo Auth admin by the owner. Production Jivo Auth to local OMS:

- **Sign-in and API:**
  - Jivo login returns 200. The token has `iss=https://auth.jivo.in`, `apps=['oms']`, and a 900 s lifetime.
  - OMS `/auth/profile/` returns 200 with **pk 124**, the same row as before the migration, and `auth_id == sub`.
  - Their party assignments return 200. `/auth/jivo-users/` returns 200: a live lookup listing 72 users, all with OMS rows.
- **Refresh:** Jivo refresh rotates both tokens, and OMS accepts the new one. After Jivo logout, refreshing that session's token returns 401.
- **Refusals:** a bad token gets 401, and the old `/api/auth/login/` gets 410.
- **Admin:**
  - The sign-in box is labelled Email. A wrong password returns the form again; the Jivo password redirects (302) to `/admin/`, which loads.
  - The user edit page has no password field and shows `auth_id`. `<id>/password/` redirects. Add user returns 403.
  - The edit page takes about 20 s against the remote DB, with the same fields as before the switch.
- **Advance-payment confirmation, against the live sessions API:**
  - An ordinary "OMS web" sign-in is refused.
  - A fresh "OMS payment confirmation" sign-in is found, then revoked; after the revoke it is refused.
  - Another user's token is refused.
- **Rows:** 112 users, 72 linked, no duplicates. The 11 active users with no Jivo link are the ones without email.
- **`manage.py check`:** clean.
- **Not tested live:** a Jivo user without `oms` getting 403 (no such account was available; covered by `users/tests_jivo_auth.py`).
- **Correction:** on 2026-10-01, `admin` and `neethu` device records came from the old OMS on 10.10.101.118, not from Jivo sign-ins.
