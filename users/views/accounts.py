"""User accounts: create, read, update, delete — and the admin lookup lists.

The remainder of `users/views.py` after auth and assignments were split out
(plan item 3.3), and it turned out to be one coherent domain rather than a
leftover: user CRUD plus the reference lists the admin screens populate their
dropdowns from (roles, states, companies, main groups, categories).

This is the group the Phase 1 security work was about. `CreateUserView` and
`UserDetailView` answered UNAUTHENTICATED requests, so anyone who could reach
the API could create an admin account or change an existing user's role.
`users.serializers.assert_may_assign_roles` is the guard that now stops role
escalation, and it is enforced here.
"""
import logging

from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.permissions import IsAdminRole
from users.serializers import UpdateUserSerializer, UserSerializer, StateSerializer, CompanySerializer, MainGroupSerializer, CreateUserSerializer, CategorySerializer
from rest_framework.generics import ListAPIView
from users.models import State, Company, MainGroup, UserRole, User
from orders.models import Categories, RateApproverRule
from ._shared import (
    _get_user_assignment_category,
)

logger = logging.getLogger(__name__)


def _selected_csv_names(value):
    return list(dict.fromkeys(
        name.strip()
        for name in str(value or '').split(',')
        if name.strip()
    ))


def _sync_rate_approver_rules(user):
    RateApproverRule.objects.filter(approver=user).delete()

    role_name = str(getattr(getattr(user, 'role', None), 'name', '') or '').strip().lower()
    category = _get_user_assignment_category(user)
    sub_groups = _selected_csv_names(getattr(user, 'sub_group', ''))

    if role_name != 'approver' or not category or not sub_groups:
        return

    for sub_group in sub_groups:
        RateApproverRule.objects.update_or_create(
            category=category,
            sub_group=sub_group,
            defaults={
                'approver': user,
                'is_active': True,
            },
        )
    
class RoleListView(APIView):
    """The role vocabulary. Any authenticated user.

    Deliberately NOT admin-only: `approvals` reads it to build the approver
    picker, and that page is open to `Payments_Dashboard` holders who are not
    administrators. It exposes role names, not who holds them.

    Had no `permission_classes` at all, which under DRF's default meant
    `AllowAny` — the silent-hole case that Phase 2.1's
    `DEFAULT_PERMISSION_CLASSES` exists to eliminate.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        roles = UserRole.objects.filter(is_active=True).values('id', 'name', 'display_name')
        return Response(list(roles))
    
class UserListForAssignmentView(APIView):
    """The full user roster. Any authenticated user.

    Was `AllowAny`, and `UserSerializer` listed `password` in its fields — so
    this endpoint handed every account's password hash to anonymous callers.
    Both halves are fixed: the hash is gone from the serializer and the roster
    now needs a login.

    Not admin-only, for the same reason as `RoleListView`: the approvals
    configuration page builds its approver picker from this and is open to
    `Payments_Dashboard` holders.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = (
            User.objects.filter(is_active=True)
            .select_related('role', 'company', 'main_group', 'state')
            .prefetch_related('main_groups', 'user_states__state')
            .order_by('id')
        )
        data = UserSerializer(users, many=True).data
        return Response({'success': True, 'data': data})

# ---------------------------------------------------------------------------
# Master data.
#
# All four were AllowAny. They are read-only reference lists rather than a
# breach on their own, but they leak the company's operating footprint — every
# state, company and product group it trades in — to anyone who finds the URL,
# and no client needs them before login: `services/api.ts` attaches the bearer
# token to every request, and every page that reads them sits behind the login
# screen.
# ---------------------------------------------------------------------------

class StateListView(ListAPIView):
    """Get all active states"""
    permission_classes = [IsAuthenticated]
    serializer_class = StateSerializer
    queryset = State.objects.filter(is_active=True).order_by('name')

class CompanyListView(ListAPIView):
    """Get all active companies"""
    permission_classes = [IsAuthenticated]
    serializer_class = CompanySerializer
    queryset = Company.objects.filter(is_active=True).order_by('name')

class MainGroupListView(ListAPIView):
    """Get all active main groups"""
    permission_classes = [IsAuthenticated]
    serializer_class = MainGroupSerializer
    queryset = MainGroup.objects.filter(is_active=True).order_by('name')
    
class CategoryListView(ListAPIView):
    """Get all categories"""
    permission_classes = [IsAuthenticated]
    serializer_class = CategorySerializer
    queryset = Categories.objects.all().order_by('category')

#Creating User
class CreateUserView(APIView):
    """Create a user account. Administrators only.

    This endpoint was `AllowAny` while its serializer accepted a `role` bound to
    `UserRole.objects.all()` — so any anonymous caller who could reach the API
    could mint themselves an `admin` account. The service is internet-facing.
    That is what this class is guarding, and it is why the serializer ALSO
    re-checks privileged-role assignment (`assert_may_assign_roles`): one guard
    protects the endpoint, the other protects the escalation.
    """

    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request):
        serializer = CreateUserSerializer(
            data=request.data, context={'request': request})

        if serializer.is_valid():
            user = serializer.save()
            _sync_rate_approver_rules(user)
            return Response({
                'success': True,
                'message': 'User created successfully',
                'data': UserSerializer(user).data
            }, status=status.HTTP_201_CREATED)

        return Response({
            'success': False,
            'message': 'Failed to create user',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)


class UserDetailView(APIView):
    """Read or update any user account. Administrators only.

    Previously `AllowAny`, which made `PUT /auth/users/<id>/` an anonymous
    account-takeover: `UpdateUserSerializer` accepts `password`, `role` and
    `is_active`, so anyone could reset the password of any account — including
    an admin's — and log in as them.

    A user reading their OWN record does not need this endpoint; `/auth/profile/`
    already serves that and is authenticated.
    """

    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
            serializer = UserSerializer(user)
            return Response({
                'success': True,
                'data': serializer.data
            }, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({
                'success': False,
                'message': 'User not found'
            }, status=status.HTTP_404_NOT_FOUND)

    def put(self, request, user_id):
 
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({
                'success': False,
          'message': 'User not found'
            }, status=status.HTTP_404_NOT_FOUND)

        serializer = UpdateUserSerializer(
            user, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            updated_user = serializer.save()
            _sync_rate_approver_rules(updated_user)
            return Response({
 
                'success': True,
                'message': 'User updated successfully',
                'data': UserSerializer(updated_user).data
            }, status=status.HTTP_200_OK)

        return Response({
            'success': False,
            'message': 'Failed to update user',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
    
class DeleteUserView(APIView):
    """Deactivate a user account (soft delete). Administrators only.

    Was `AllowAny`: an anonymous caller could walk the ID range and disable
    every account in the company, including every admin, locking the business
    out of its own system.
    """

    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
            # Deactivating yourself is how an admin locks themselves out with a
            # misclick; there is no undo through the API once the last admin is
            # disabled.
            if user.pk == request.user.pk:
                return Response(
                    {'success': False,
                     'message': 'You cannot deactivate your own account'},
                    status=status.HTTP_400_BAD_REQUEST)
            user.is_active = False
            user.save()
            return Response({'success': True, 'message': 'User removed successfully'}, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'}, status=status.HTTP_404_NOT_FOUND)
