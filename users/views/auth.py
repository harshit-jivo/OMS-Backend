"""Authentication: what is left of it in OMS, and what the UI may show.

Users sign in at Jivo Auth (auth.jivo.in), not here. Every request carries a
Jivo access token, verified by `jivo_auth.authentication.JivoJWTAuthentication`
(settings.REST_FRAMEWORK), which hands the view the OMS `users.User` row whose
`auth_id` is the token's subject. See docs/jivo-auth-integration.md.

OMS's own login, token refresh and logout are gone with it. `/auth/login/`
stays only as `JivoLoginGoneView`, answering 410 with where to sign in, for
clients that haven't moved yet (the React Native OMS-app ships separately).
It is the one endpoint here that answers unauthenticated requests, and
`core.tests.PublicEndpointAllowlistTests` fails if another appears.

`ProfileView` is how a client learns which OMS user it is after signing in at
Jivo Auth: the Jivo token names the Jivo user, while every OMS screen keys on
the OMS id, roles and pages this returns.

`PagePermissionsView` decides which pages a user is offered. It answers by
role, and deliberately does NOT treat a superuser as an admin — see
`core/permissions.py`, which documents that difference as the one place the
project's several definitions of "admin" legitimately diverge.
"""

import logging
from django.conf import settings
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from core.permissions import is_admin
from users.serializers import UserSerializer
from users.models import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenAPI response shapes — documentation only, no runtime effect.
#
# These views assemble their JSON by hand instead of declaring a
# `serializer_class`, so drf-spectacular has nothing to infer from and emits an
# undescribed body; the frontend generates its TypeScript from this document,
# so an undescribed body means an untyped `data`. Each declaration below mirrors
# the literal dict the method returns — envelope included — because a *wrong*
# declaration is worse than none: the compiler would then agree with the lie.
# ---------------------------------------------------------------------------

#: `/api/auth/login/` 410, whatever the method.
LOGIN_GONE_RESPONSE = inline_serializer(name='LoginGone', fields={
    'success': serializers.BooleanField(),
    'message': serializers.CharField(),
    'sign_in_url': serializers.CharField(),
})

#: `GET /api/auth/profile/` 200. Deliberately has no `message` key — the view
#: returns only `success` and `data`, unlike most envelopes in this app.
PROFILE_RESPONSE = inline_serializer(name='Profile', fields={
    'success': serializers.BooleanField(),
    'data': UserSerializer(),
})


def jivo_sign_in_url():
    return f"{settings.JIVO_AUTH['URL'].rstrip('/')}/api/v1/auth/login/"


@extend_schema(
    request=None,
    responses={410: LOGIN_GONE_RESPONSE},
    description='Retired. OMS no longer signs anyone in: sign in at Jivo Auth '
                '(`sign_in_url`) and send its access token as '
                '`Authorization: Bearer`.',
)
class JivoLoginGoneView(APIView):
    """The old `/auth/login/`: 410 Gone, saying where sign-in lives now.

    `AllowAny` with no authentication, so a client still holding an old OMS
    token gets this answer rather than a 401 it would try to refresh. It
    checks no credentials and never reads the body, so it needs no login
    throttle; the global `anon` rate still applies.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def _gone(self, request, *args, **kwargs):
        return Response({
            'success': False,
            'message': 'OMS sign-in has moved to Jivo Auth. Sign in there '
                       'with your email and password.',
            'sign_in_url': jivo_sign_in_url(),
        }, status=status.HTTP_410_GONE)

    get = post = put = patch = delete = _gone

@extend_schema(
    responses={200: PROFILE_RESPONSE},
    description='The authenticated session identity. `data` is the full '
                '`UserSerializer` record for `request.user` — roles, '
                'assignments and `extra_pages` — which the frontend route '
                'guards read. Single code path: no error branch of its own.',
)
class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'success': True,
            'data': UserSerializer(request.user).data
        })


class PagePermissionsView(APIView):
    """Admin-managed per-user page access (list of page keys)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        # Reading someone else's granted pages tells you what they can reach;
        # only an admin needs that, and every user can read their own.
        if user_id != request.user.pk and not is_admin(request.user):
            return Response(
                {'success': False,
                 'message': 'You may only view your own page permissions'},
                status=status.HTTP_403_FORBIDDEN)
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({
            'success': True,
            'data': {'user_id': user.id, 'extra_pages': user.extra_pages or []},
        })

    def put(self, request, user_id):
        # `core.permissions.is_admin` replaces a local `_is_admin` that read the
        # `role` FK alone — so a user granted `admin` through `extra_roles` was
        # refused here while `payments` and `approvals` accepted them.
        if not is_admin(request.user):
            return Response({'success': False, 'message': 'Only admin can change page permissions'},
                            status=status.HTTP_403_FORBIDDEN)
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'},
                            status=status.HTTP_404_NOT_FOUND)

        pages = request.data.get('extra_pages', [])
        if not isinstance(pages, list):
            return Response({'success': False, 'message': 'extra_pages must be a list'},
                            status=status.HTTP_400_BAD_REQUEST)

        cleaned = []
        for page in pages:
            page = str(page).strip()
            if page and page not in cleaned:
                cleaned.append(page)

        user.extra_pages = cleaned
        user.save(update_fields=['extra_pages', 'updated_at'])
        return Response({
            'success': True,
            'message': 'Page permissions updated',
            'data': {'user_id': user.id, 'extra_pages': cleaned},
        })
