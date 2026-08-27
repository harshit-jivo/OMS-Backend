"""Order views.

This was a single 5,919-line `orders/views.py`. It is being split by domain
(plan item 3.1), one module at a time, and this package is the seam that lets
that happen without touching a single caller: `orders.views.<anything>` still
resolves exactly as it did.

    _shared.py         scoping helpers several view groups need
    notifications.py   notification views + recipient resolution
    _legacy.py         everything not yet moved — meant to shrink to nothing

Why the re-export below is written the way it is: `from ._legacy import *`
skips names beginning with an underscore, and this module's private helpers are
imported by name from outside it — `orders.tests_scheme_engine` reaches for
`_extract_order_item_schemes` and `_apply_engine_schemes`. Dropping them would
break those imports, so the loop below carries them across too.

WHAT THIS PACKAGE CANNOT PRESERVE, and the reason it is called out here: a name
re-exported into this namespace is a SEPARATE BINDING from the one its own
module uses. `mock.patch('orders.views.some_helper')` replaces the copy here,
while the code that calls it — living in `_legacy` or `notifications` — goes on
using its own. The patch silently does nothing and the test passes anyway.
Patch where a name is LOOKED UP: `orders.views.notifications.<name>`.
"""
from ._legacy import *  # noqa: F401,F403
from .notifications import *  # noqa: F401,F403

from . import _legacy, _shared, notifications  # noqa: F401

# Carry the underscore-prefixed helpers across as well; see the docstring.
_ns = globals()
for _module in (_shared, notifications, _legacy):
    for _name in dir(_module):
        if _name.startswith('_') and not _name.startswith('__'):
            _ns.setdefault(_name, getattr(_module, _name))
del _ns, _module, _name
