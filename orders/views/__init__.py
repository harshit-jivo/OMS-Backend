"""Order views.

This was a single 5,919-line `orders/views.py`. It is being split by domain
(plan item 3.1), one module at a time, and this package is the seam that lets
that happen without touching a single caller: `orders.views.<anything>` still
resolves exactly as it did.

    crystal.py         sales order print, rendered by the Crystal service
    lifecycle.py       placing an order and moving it through its flow
    queries.py         read-only order lists, details, logs, status tracking
    dashboards.py      dashboard/reporting views (read-only)
    masters.py         parties, products, addresses, dispatch locations
    schemes.py         scheme administration (v1 and v2)
    mart.py            distributor (Mart) orders — the SAP-writing path
    notifications.py   notification views + recipient resolution
    flow_config.py     editing the status flow configuration
    templates.py       saved order templates
    stock.py           pre-order stock check
    ai.py              AI order summary
    _shared.py         scoping helpers and status constants several groups need

There is no `_legacy` module any more: the split finished, and what was left
turned out to be one domain (the write path) rather than a remainder.

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
from .ai import *  # noqa: F401,F403
from .crystal import *  # noqa: F401,F403
from .flow_config import *  # noqa: F401,F403
from .lifecycle import *  # noqa: F401,F403
from .queries import *  # noqa: F401,F403
from .stock import *  # noqa: F401,F403
from .templates import *  # noqa: F401,F403
from .dashboards import *  # noqa: F401,F403
from .schemes import *  # noqa: F401,F403
from .mart import *  # noqa: F401,F403
from .masters import *  # noqa: F401,F403
from .notifications import *  # noqa: F401,F403

from . import (  # noqa: F401
    _shared, ai, crystal, dashboards, flow_config, lifecycle, mart, masters,
    notifications, queries, schemes, stock, templates,
)

# Carry the underscore-prefixed helpers across as well; see the docstring.
_ns = globals()
for _module in (_shared, notifications, dashboards, schemes, mart, masters,
                flow_config, queries, templates, stock, ai, lifecycle):
    for _name in dir(_module):
        if _name.startswith('_') and not _name.startswith('__'):
            _ns.setdefault(_name, getattr(_module, _name))
del _ns, _module, _name
