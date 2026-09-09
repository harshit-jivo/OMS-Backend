"""Pagination for the new apps.

The project sets no DEFAULT_PAGINATION_CLASS, so every existing list endpoint
returns the whole table. Rather than change that globally (which would reshape
every existing response and break clients), the new apps opt in explicitly.
"""
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 200

    def get_paginated_response(self, data):
        # Wrapped in the same {success, message, data} envelope the rest of the
        # new API uses, so a client never has to special-case list endpoints.
        return Response({
            'success': True,
            'message': '',
            'data': {
                'results': data,
                'pagination': {
                    'page': self.page.number,
                    'page_size': self.get_page_size(self.request),
                    'total': self.page.paginator.count,
                    'total_pages': self.page.paginator.num_pages,
                },
            },
        })


def ordering_from(request, allowed, default):
    """Resolve an ?ordering= param against an ALLOW-LIST.

    Copied from devices/admin_views.py:53-67, whose comment is worth repeating:
    never interpolate user input into order_by().
    """
    ordering = (request.query_params.get('ordering') or default).strip()
    if ordering.lstrip('-') not in allowed:
        return default
    return ordering


class OptInPagination(StandardPagination):
    """Pagination a client chooses, rather than one imposed on it — Phase 6.3.

    The problem 6.3 names is real: several list endpoints return whole tables.
    `GET /api/sap/party-addresses/` with no filter serialises all 35,719 rows
    of `sap_sync.PartyAddress` on every call.

    The obvious fix is not available. Turning on pagination changes the
    response from a JSON array into an object, and the live web and mobile
    clients index straight into the array — so `DEFAULT_PAGINATION_CLASS`
    would break every list endpoint at once, which is exactly the class of
    change Phase 6.2 exists to prevent.

    So this paginates ONLY when asked. No `?page=` or `?page_size=` in the
    query string and `paginate_queryset` returns None, which makes DRF render
    the plain list exactly as it does today — byte for byte. A client that
    passes `?page=1` gets the paginated envelope and can migrate on its own
    schedule, without waiting for anyone else.

    This is a migration path, not the destination. The destination is
    pagination by default under a future `/api/v2/`, where changing the
    response shape is what a version bump is FOR.
    """

    def paginate_queryset(self, queryset, request, view=None):
        asked = (self.page_query_param in request.query_params
                 or self.page_size_query_param in request.query_params)
        if not asked:
            _warn_if_unbounded(queryset, request)
            return None
        return super().paginate_queryset(queryset, request, view)


#: A response larger than this is logged. Not a limit — truncating silently
#: would be a correctness bug dressed up as a performance fix. It makes the
#: cost visible so the migration can be prioritised by evidence: which
#: endpoints, how large, and which client is asking.
UNBOUNDED_WARN_THRESHOLD = 1000


def _warn_if_unbounded(queryset, request):
    import logging

    logger = logging.getLogger('core.pagination')
    try:
        count = queryset.count()
    except Exception:
        return
    if count < UNBOUNDED_WARN_THRESHOLD:
        return
    logger.warning(
        'unpaginated list response: %s rows from %s', count, request.path,
        extra={
            'row_count': count,
            'endpoint': request.path,
            # Which client is still asking for the whole table — the field
            # that says when pagination can become the default.
            'client_platform': request.META.get('HTTP_X_PLATFORM', ''),
            'client_version': request.META.get('HTTP_X_APP_VERSION', ''),
        },
    )
