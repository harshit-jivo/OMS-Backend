"""Complete endpoint inventory, generated from the live URLconf.

Walks the resolver rather than reading `urls.py` by hand, so nothing is missed
and nothing is inferred from naming. For every route: path, view, HTTP methods,
the permission classes that ACTUALLY apply, and the view's first docstring line.

Two things it does that a grep for `permission_classes` cannot:

* It resolves the DRF default. A view that declares nothing inherits
  `DEFAULT_PERMISSION_CLASSES`, so whether it is open depends on settings, not
  on the view file. Grep reports "no declaration"; this reports the truth.
* It sees every route, including ones reached through a router or an `include`
  whose urls.py nobody has read recently.

Usage::

    python scripts/endpoint_inventory.py > docs/codebase/API_SURFACE.md
    python scripts/endpoint_inventory.py --open-only     # just the open ones
    python scripts/endpoint_inventory.py --json          # machine-readable

On Windows set PYTHONIOENCODING=utf-8, or the box-drawing output dies on cp1252.
"""
import inspect
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OMS.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.urls import get_resolver  # noqa: E402
from django.urls.resolvers import URLPattern, URLResolver  # noqa: E402
from rest_framework.settings import api_settings  # noqa: E402

HTTP = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options')

LOCAL_APPS = tuple(
    a for a in settings.INSTALLED_APPS if not a.startswith('django.')
    and '.' not in a and a not in {'rest_framework', 'corsheaders'}
)

# Permission markers that leave a route reachable without a login.
PLAIN_DJANGO = '(plain Django view — DRF does not apply)'
OPEN_PERMISSIONS = {'AllowAny', PLAIN_DJANGO}

DEFAULT_PERMS = [c.__name__ for c in api_settings.DEFAULT_PERMISSION_CLASSES]


def walk(resolver, prefix=''):
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            yield from walk(p, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback, p.name


def describe(cb):
    """(view_name, methods, permissions, declared?, docstring-first-line)."""
    cls = getattr(cb, 'view_class', None) or getattr(cb, 'cls', None)
    if cls is not None:
        methods = sorted(m.upper() for m in HTTP if hasattr(cls, m))
        # "Declared" means OUR code set it. Walking the whole MRO is wrong:
        # `rest_framework.views.APIView` defines `permission_classes` itself
        # (that is how the default is delivered), so every class-based view
        # would look explicitly declared and the count of routes relying on the
        # default would come out as zero. Stop at the framework boundary.
        declared = any(
            'permission_classes' in vars(klass)
            for klass in type.mro(cls)
            if not klass.__module__.startswith('rest_framework')
        )
        perms = [c.__name__ for c in getattr(cls, 'permission_classes', [])]
        doc = (inspect.getdoc(cls) or '').strip().splitlines()
        return cls.__name__, methods, perms, declared, (doc[0] if doc else '')

    wrapped = getattr(cb, '__wrapped__', cb)
    doc = (inspect.getdoc(wrapped) or '').strip().splitlines()
    name = getattr(wrapped, '__name__', str(cb))

    if not hasattr(cb, 'cls') and not hasattr(cb, 'initkwargs'):
        # A PLAIN Django view — no @api_view, so it never enters DRF's dispatch
        # at all. DRF authentication, permissions and throttling are all
        # inapplicable, which means DEFAULT_PERMISSION_CLASSES cannot close it
        # and no amount of settings work will. It has to be converted to a DRF
        # view or grow its own check. Reported as its own category precisely
        # because it is invisible to every permission audit that greps for
        # `permission_classes`.
        return name, ['*'], ['(plain Django view — DRF does not apply)'], True, \
            (doc[0] if doc else '')

    # Function-based @api_view.
    initkw = getattr(cb, 'initkwargs', {}) or {}
    declared = 'permission_classes' in initkw
    perms = [c.__name__ for c in initkw.get('permission_classes', [])]
    methods = sorted(getattr(cb, 'actions', {}) or {})
    return name, methods, perms, declared, (doc[0] if doc else '')


rows = []
for path, cb, name in walk(get_resolver()):
    mod = getattr(cb, '__module__', '') or ''
    if not mod.startswith(LOCAL_APPS):
        continue
    view, methods, perms, declared, doc = describe(cb)
    effective = perms if declared else DEFAULT_PERMS
    rows.append({
        'path': '/' + path,
        'name': name or '',
        'app': mod.split('.')[0],
        'view': view,
        'methods': ','.join(methods) or '?',
        'permissions': effective,
        'declared': declared,
        # A route is open when EVERY applying class is permissive. DRF ANDs
        # them, so one restrictive class is enough to close it.
        'open': bool(effective) and all(p in OPEN_PERMISSIONS for p in effective),
        'doc': doc[:110],
    })

rows.sort(key=lambda r: (r['app'], r['path']))
open_rows = [r for r in rows if r['open']]

if '--json' in sys.argv:
    json.dump(rows, sys.stdout, indent=2)
    sys.exit(0)

if '--open-only' in sys.argv:
    print(f'{len(open_rows)} of {len(rows)} routes require no authentication.')
    print(f'DEFAULT_PERMISSION_CLASSES = {DEFAULT_PERMS or "(unset -> AllowAny)"}\n')
    plain = [r for r in open_rows if PLAIN_DJANGO in r['permissions']]
    inherited = [r for r in open_rows if not r['declared'] and r not in plain]
    explicit = [r for r in open_rows if r['declared'] and r not in plain]

    print(f'  {len(inherited):>3} inherit the default   '
          '-> closed by setting DEFAULT_PERMISSION_CLASSES')
    print(f'  {len(explicit):>3} declare AllowAny      '
          '-> each declaration must be removed')
    print(f'  {len(plain):>3} plain Django views    '
          '-> DRF cannot reach these at all\n')

    for label, group in (('INHERITED', inherited), ('EXPLICIT AllowAny', explicit),
                         ('PLAIN DJANGO', plain)):
        if not group:
            continue
        print(f'--- {label} ---')
        for r in group:
            print(f"  {r['app']:<14} {r['path']:<62} {r['view']}")
        print()
    sys.exit(0)

print(f'# Endpoint inventory — {len(rows)} routes\n')
print('Generated from the live URLconf by `scripts/endpoint_inventory.py`.')
print('Regenerate after any routing or permission change.\n')
print(f'**`DEFAULT_PERMISSION_CLASSES` = '
      f'`{DEFAULT_PERMS or "(unset — DRF falls back to AllowAny)"}`**\n')
print('A view that declares no `permission_classes` inherits that default, so')
print('the "permissions" column shows what APPLIES, not what the view file says.')
print('`(inherited)` marks the ones relying on the default.\n')
print(f'> **{len(open_rows)} of {len(rows)} routes are reachable without '
      f'authentication.**\n')

cur = None
for r in rows:
    if r['app'] != cur:
        cur = r['app']
        n = sum(1 for x in rows if x['app'] == cur)
        opn = sum(1 for x in rows if x['app'] == cur and x['open'])
        flag = f', **{opn} open**' if opn else ', all authenticated'
        print(f'\n## `{cur}` — {n} routes{flag}\n')
        print('| path | methods | view | permissions | note |')
        print('|---|---|---|---|---|')
    perms = ','.join(r['permissions']) or '(none)'
    if not r['declared']:
        perms += ' _(inherited)_'
    flag = ' 🔴' if r['open'] else ''
    print(f"| `{r['path']}` | {r['methods']} | `{r['view']}` | {perms}{flag} | {r['doc']} |")

print(f'\n---\n\n**Totals: {len(rows)} routes, {len(open_rows)} reachable '
      f'without authentication.**')
