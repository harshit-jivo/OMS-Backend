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
