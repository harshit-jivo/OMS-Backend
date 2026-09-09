"""OpenAPI schema shaping — Phase 6.2.

The API is mounted at two prefixes (`/api/...` and `/api/v1/...`, see
OMS/urls.py). Both are live, but the schema must describe ONE of them, for two
reasons:

* A document listing every endpoint twice is nearly useless — a reader cannot
  tell which of the two to call, and a generated client would produce two
  methods per operation with identical bodies.
* Every `operationId` would collide, and drf-spectacular resolves collisions by
  appending numeric suffixes. The published contract would then name operations
  `orders_submit_create` and `orders_submit_create_2`, with nothing to say
  which is which.

`/api/v1/` is the one described, because the published contract should name the
prefix new clients are meant to call. The unversioned prefix keeps working and
is documented in prose (OMS/urls.py) rather than in the schema.
"""
import re

#: The prefix the schema describes. Anything under `/api/` that is NOT under
#: this is the legacy alias of a route already described.
VERSIONED_PREFIX = '/api/v1/'

_UNVERSIONED = re.compile(r'^/api/(?!v1/|schema/)')


def only_versioned_routes(endpoints, **kwargs):
    """Drop the unversioned alias of each route from the generated schema.

    A preprocessing hook, so it runs before operations are built and the
    dropped paths never reach `operationId` assignment — which is where the
    collisions would otherwise appear.

    Deliberately filters by PATH rather than by resolved view: the two mounts
    share the same view classes, so a view-based filter could not tell them
    apart and would drop both.
    """
    return [
        (path, path_regex, method, callback)
        for path, path_regex, method, callback in endpoints
        if not _UNVERSIONED.match(path)
    ]
