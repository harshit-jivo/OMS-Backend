"""Permissions for the device-management admin API.

This module used to define its own `is_admin_user`, under a docstring claiming
it mirrored the others "so there is exactly one definition of admin behaviour
across the codebase". It did not: it was one of FIVE definitions that disagreed
with each other. This one ignored `is_superuser`, so a Django superuser could
edit UI labels but not read device policy — a difference nobody chose.

The real single definition now lives in `core.permissions`. Both names are
re-exported so existing imports keep working; the behaviour they resolve to has
widened to include `is_superuser`, `is_staff` and roles held through
`extra_roles`.

NOTE, unchanged and still true: the device tables expose every user's device
names, activity and software inventory across the org, so these endpoints must
never be AllowAny.
"""
from core.permissions import IsAdminRole, is_admin as is_admin_user

__all__ = ['IsAdminRole', 'is_admin_user']
