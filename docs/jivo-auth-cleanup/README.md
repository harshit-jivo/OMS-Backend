# Jivo Auth cleanup — staged release (phase 6)

**Status: prepared 2026-10-01, NOT applied anywhere.** It deletes data, and
the change to passwords can't be undone except from a backup. Applying it
needs the owner's explicit approval of this exact list, at the time.

Background is in [../jivo-auth-integration.md](../jivo-auth-integration.md).

## Why it waits

On 2026-10-01 the shared test DB (`order_management` on 138.252.101.117) was
still used by an OMS on **10.10.101.118 running the old code**, which signs
people in with OMS passwords and SimpleJWT. It issued 26 OMS tokens in the
last 24 hours. Running this cleanup there would:

- lock everyone out of that OMS, because their passwords become unusable;
- break its login and logout outright, because the `token_blacklist`
  tables are gone.

## Preconditions — all must hold

1. **Every OMS that uses the database has the Jivo Auth switch deployed.**
   That includes the server on 10.10.101.118 and any other server pointing
   at the same DB. Check that no non-switched client is connected:
   ```sql
   SELECT client_addr, count(*) FROM pg_stat_activity
   WHERE datname = 'order_management' GROUP BY 1;
   ```
   Also confirm that no new SimpleJWT tokens are being issued:
   ```sql
   SELECT max(created_at) FROM token_blacklist_outstandingtoken;  -- must be before the switch
   ```
2. **The switch has run cleanly for a while.** Suggested: two weeks, with
   no rollback.
3. **The React Native OMS-app signs in with Jivo Auth.** Until it does,
   release B below keeps the 410 login stub.
4. **Every active user who signs in has an `auth_id`, or each exception is
   known.** On 2026-10-01 the exceptions were 11 users without email (pks
   125, 136–139, 351, 352, 358–361). Check:
   ```bash
   python manage.py shell -c "from users.models import User; print(list(User.objects.filter(is_active=True, auth_id__isnull=True).values_list('pk', 'username')))"
   ```
5. **No code reads what's removed.** Search again, because this list was
   written weeks before it's used:
   `git grep -n "OutstandingToken\|BlacklistedToken\|token_blacklist\|set_password\|check_password" -- '*.py' ':!*/migrations/*'`.
   On 2026-10-01 only `OMS/settings.py` referenced `token_blacklist`. Every
   `check_password` and `set_password` is in tests or this folder.
6. **Staff are told** that their admin sessions end and they sign in again
   with their Jivo email.

## What it changes

At preparation time:

| Item | Change | Size on .117 (2026-10-01) |
|---|---|---|
| `users_user.password` | Every usable hash becomes a random unusable value, one per row. The column stays. | 112 of 112 rows |
| `token_blacklist_outstandingtoken` | Rows deleted, table dropped | 1973 rows |
| `token_blacklist_blacklistedtoken` | Rows deleted (cascade), table dropped | 1140 rows |
| `django_migrations` | 12 `token_blacklist` rows removed by `migrate token_blacklist zero` | 12 |
| Content types and permissions of those two models | Removed by `remove_stale_contenttypes` | — |

Unchanged: every user row, its primary key, `auth_id`, `last_login`,
`is_active`, staff flags, roles, assignments and every business table.

There are no other authentication-only columns: OMS had no OTP, lockout or
verification fields (analysis, section 2).

## Release A — passwords and token tables

Run with the code that still has `rest_framework_simplejwt.token_blacklist`
in `INSTALLED_APPS`, which is the switch release as it stands.

```bash
# 0. Backup. It holds every password hash: mode 600, never in the repo.
umask 077
pg_dump -Fc -h <host> -U <user> -d order_management \
  -f order_management-before-jivo-cleanup-$(date +%F-%H%M).dump
pg_restore -l order_management-before-jivo-cleanup-*.dump | grep -c "TABLE DATA"   # sanity

# 1. Passwords. Copy the staged migration in, renumbered after the latest one.
cp docs/jivo-auth-cleanup/0039_disable_local_passwords.py users/migrations/
#    Edit `dependencies` to the latest users migration. Rename it if 0039 is taken.
python manage.py test users.tests_jivo_cleanup --settings=OMS.test_settings
python manage.py migrate users

# 2. Token tables. Empty them first: token_blacklist's reverse migrations
#    stop with "IntegrityError: NOT NULL ... jti" on existing rows.
python manage.py shell -c "from rest_framework_simplejwt.token_blacklist.models import OutstandingToken; print(OutstandingToken.objects.all().delete())"
python manage.py migrate token_blacklist zero
#    If a reverse migration fails anyway: restore the backup. Don't edit
#    django_migrations by hand.

# 3. Leftover content types and permissions (it lists them and asks first).
python manage.py remove_stale_contenttypes
```

Then check:
```bash
python manage.py shell -c "from users.models import User; print('usable passwords left:', sum(u.has_usable_password() for u in User.objects.all()))"   # 0
python manage.py check
```
Finally, run phase 5 again: sign in at Jivo, the API, and the admin.

`tests_jivo_cleanup.test_it_is_staged_not_active` fails once the migration
is in `users/migrations/`. That's expected: delete that test in the same
release.

## Release B — code (after release A has run in every environment)

1. `OMS/settings.py`: remove `'rest_framework_simplejwt.token_blacklist'`
   from `INSTALLED_APPS`, and its comment next to `JIVO_AUTH`.
2. `requirements.txt`: remove `djangorestframework_simplejwt` if nothing
   else imports it (`git grep -n rest_framework_simplejwt`).
3. **Only once the mobile app has moved:**
   - in `users/urls.py`, `users/views/auth.py` and `users/views/__init__.py`,
     remove `JivoLoginGoneView` and the `login/` route;
   - in `core/tests.py`, remove `/api/auth/login/` from `PUBLIC_ROUTES`;
   - in `users/tests.py`, `SignInMovedTests` changes to expect a 404.
4. Run `python manage.py check`, `makemigrations --check`, and the suite.

## Rollback

- **Release B:** redeploy the previous code.
- **Release A:** restore the backup from step 0, the only copy of the old
  hashes and tokens. Before that point, nothing in this folder has run.
