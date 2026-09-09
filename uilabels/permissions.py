"""Permissions for the UI-label admin API.

Like `devices.permissions`, this module carried its own `is_admin_user` under a
docstring claiming there was "exactly one definition of admin behaviour across
the codebase". There were five, and they disagreed — this one honoured
`is_superuser` while the devices copy did not.

`core.permissions` is that one definition now. Re-exported so existing imports
keep working.

Reading labels is open to any authenticated user (they drive the UI for
everyone); only editing is admin-gated. That rule is enforced in `uilabels/views.py`.
"""
from core.permissions import IsAdminRole, is_admin as is_admin_user

__all__ = ['IsAdminRole', 'is_admin_user']
