"""User views: authentication, account administration, and assignments.

`users/views.py` was 1,381 lines covering three unrelated jobs (plan item 3.3).
This package is the seam that let it be split without touching `users/urls.py`
or any caller: `users.views.<anything>` still resolves.

    auth.py             login, token refresh, logout, profile, page permissions
    assignments.py      which users may sell which parties, and which products
    accounts.py         user CRUD and the lookup lists the admin screens need
    role_permissions.py the Role Permissions matrix: registry + role bundles
    _shared.py          the one helper two of those three need

The same patching caveat as `orders.views` applies: a name re-exported here is
a SEPARATE BINDING from the one its own module uses, so
`mock.patch('users.views.helper')` would replace the copy here while the code
that calls it goes on using its own — the patch silently does nothing and the
test passes. Patch where a name is looked up, e.g. `users.views.auth.helper`.
Nothing patches these today; this note is for when something does.
"""
from .accounts import *  # noqa: F401,F403
from .assignments import *  # noqa: F401,F403
from .auth import *  # noqa: F401,F403
from .role_permissions import (  # noqa: F401
    PermissionRegistryView,
    RoleCreateView,
    RoleDeleteView,
    RolePermissionsListView,
    RolePermissionsUpdateView,
    RoleUpdateView,
)

from . import _shared, accounts, assignments, auth  # noqa: F401

_ns = globals()
for _module in (_shared, auth, assignments, accounts):
    for _name in dir(_module):
        if _name.startswith('_') and not _name.startswith('__'):
            _ns.setdefault(_name, getattr(_module, _name))
del _ns, _module, _name
