"""One response envelope for the new apps.

The existing API has five competing shapes ({success,message,data}, bare data,
raw SAP passthrough, ad-hoc keys, nested results) and five error keys
(message / error / detail / details / error+detail), so a client cannot write
one handler. The new apps standardise on the majority convention — the
{success, message, data} envelope used by `users`, `devices` and `uilabels`.

Existing endpoints are deliberately left alone; retrofitting them would break
the live mobile and web clients.
"""
from rest_framework import status as http_status
from rest_framework.response import Response


def ok(data=None, message='', status=http_status.HTTP_200_OK):
    return Response({'success': True, 'message': message, 'data': data}, status=status)


def created(data=None, message='Created.'):
    return ok(data, message, status=http_status.HTTP_201_CREATED)


def fail(message, *, errors=None, status=http_status.HTTP_400_BAD_REQUEST):
    """Error envelope. `errors` carries field-level detail when it exists."""
    body = {'success': False, 'message': message, 'data': None}
    if errors is not None:
        body['errors'] = errors
    return Response(body, status=status)
