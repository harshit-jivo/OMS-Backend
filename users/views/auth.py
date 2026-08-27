"""Authentication: login, token refresh, logout, and what the UI may show.

Split out of `users/views.py` (plan item 3.3). These are the only endpoints in
the project that answer unauthenticated requests — `LoginView` and
`AuthTokenRefreshView` declare `AllowAny` explicitly, and
`core.tests.PublicEndpointAllowlistTests` fails if a third one appears.

Both are throttled by the `login` scope rather than the general `anon` bucket,
because they check credentials: this is the brute-force control.

`PagePermissionsView` decides which pages a user is offered. It answers by
role, and deliberately does NOT treat a superuser as an admin — see
`core/permissions.py`, which documents that difference as the one place the
project's several definitions of "admin" legitimately diverge.
"""

import logging
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from core.permissions import is_admin
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth.models import update_last_login
from users.serializers import LoginSerializer, UserSerializer
from users.models import User

logger = logging.getLogger(__name__)



class LoginView(APIView):
    """Exchange credentials for a JWT pair.

    Necessarily `AllowAny`, and therefore the one endpoint that most needs a
    rate limit: nothing throttled it, so passwords could be guessed at whatever
    speed the network allowed. `ScopedRateThrottle` applies the `login` rate
    from settings (10/min per IP by default) rather than the looser `anon` one.

    Keyed by IP, which is the only identifier available before authentication.
    A shared office NAT therefore shares one bucket — the rate is set high
    enough that ordinary humans never reach it and low enough that guessing is
    hopeless.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        serializer = LoginSerializer(data=request.data)

        if serializer.is_valid():
            user = serializer.validated_data['user']
            refresh = RefreshToken.for_user(user)
            update_last_login(None, user)

            access_lifetime = jwt_settings.ACCESS_TOKEN_LIFETIME
            logger.info("Login successful user_id=%s", user.id)

            return Response({
                'success': True,
                'message': 'Login successful',
                'data': {
                    'user': UserSerializer(user).data,
                    # Existing fields kept exactly (clients read data.tokens.*);
                    # token_type + expires_in are additive (Task 4).
                    'tokens': {
                        'access': str(refresh.access_token),
                        'refresh': str(refresh),
                        'token_type': 'Bearer',
                        'expires_in': int(access_lifetime.total_seconds()),
                    },
                }
            })

        logger.warning(
            "Login failed for username=%s",
            str(request.data.get('username', ''))[:150],
        )
        return Response({
            'success': False,
            'message': 'Login failed',
            'errors': serializer.errors
        }, status=status.HTTP_401_UNAUTHORIZED)


class ActiveUserTokenRefreshSerializer(TokenRefreshSerializer):
    """Refresh serializer that also rejects tokens whose user is now missing or
    inactive (Task 7). The parent already handles expired/invalid tokens and,
    with BLACKLIST_AFTER_ROTATION, rotates + blacklists the old refresh token.
    """

    def validate(self, attrs):
        # Decode/verify the refresh token BEFORE rotation so we can look up the
        # subject. This raises for expired/invalid/blacklisted tokens.
        token = RefreshToken(attrs['refresh'])
        user_id = token.get(jwt_settings.USER_ID_CLAIM)
        try:
            user = User.objects.get(**{jwt_settings.USER_ID_FIELD: user_id})
        except User.DoesNotExist:
            raise InvalidToken('No active account found for this token')
        if not user.is_active:
            raise InvalidToken('User account is disabled')

        return super().validate(attrs)


class AuthTokenRefreshView(TokenRefreshView):
    """POST /api/auth/refresh/ — exchange a refresh token for a new access
    token, honouring rotation/blacklist settings and blocking inactive users.
    """

    permission_classes = [AllowAny]
    serializer_class = ActiveUserTokenRefreshSerializer
    # Also credential-checking and also unauthenticated: the body carries a
    # refresh token, so an unthrottled endpoint is a token-guessing oracle.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'


class LogoutView(APIView):
    """POST /api/auth/logout/ — blacklist the refresh token so it cannot be
    reused (server-side invalidation, not just a client-side clear)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = (
            request.data.get('refresh')
            or request.data.get('refresh_token')
            or ''
        )
        if not refresh_token:
            return Response(
                {'success': False, 'message': 'Refresh token is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError:
            # Already expired/invalid/blacklisted — logout is idempotent.
            logger.info("Logout with already-invalid refresh user_id=%s", request.user.id)
            return Response({'success': True, 'message': 'Logged out'})

        logger.info("Logout success user_id=%s", request.user.id)
        return Response({'success': True, 'message': 'Logged out'})

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
