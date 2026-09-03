"""The Role Permissions matrix: what each role can do, readable and editable.

This is the screen the permission system never had. `PagePermissionsView`
edits one USER's grants; nothing showed what a ROLE confers — that lived in
hardcoded `role_name == '...'` comparisons that only a deploy could change.
These endpoints put role authority where an admin can see and change it:

    GET /api/auth/permission-registry/   the registry, grouped for rendering
    GET /api/auth/roles/permissions/     every role with its bundle
    PUT /api/auth/roles/<id>/permissions/  replace one role's bundle

Changing a bundle takes effect on each holder's next `effective_keys`
resolution — their next request — with no deploy and no re-login for API
enforcement (the frontend refreshes its copy on next profile load).

Unknown keys are REJECTED on write, not filtered: an admin posting a typo'd
key deserves an error naming it, not a tick that silently never grants.
(`effective_keys` additionally ignores unknown stored keys on read, so stale
data left by a registry change is inert rather than poisonous.)

The registry endpoint is admin-only like the rest, deliberately: ordinary
clients get their answer from `permissions` on the login/profile payload and
have no use for the full catalogue of what exists to be granted.
"""

from django.db import DatabaseError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permission_registry import ALL_KEYS, REGISTRY
from core.permissions import IsAdminRole
from users.models import RolePermissions, User, UserRole

REGISTRY_RESPONSE = inline_serializer(name='PermissionRegistry', fields={
    'success': serializers.BooleanField(),
    'data': inline_serializer(name='PermissionRegistryData', fields={
        'modules': serializers.ListField(child=inline_serializer(
            name='PermissionRegistryModule', fields={
                'name': serializers.CharField(),
                'keys': serializers.ListField(child=inline_serializer(
                    name='PermissionRegistryKey', fields={
                        'key': serializers.CharField(),
                        'label': serializers.CharField(),
                    })),
            })),
    }),
})


@extend_schema(responses={200: REGISTRY_RESPONSE},
               description='The permission registry, grouped by module for the '
                           'matrix page. Static per deploy; changes only with code.')
class PermissionRegistryView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        return Response({
            'success': True,
            'data': {
                'modules': [
                    {
                        'name': module,
                        'keys': [{'key': k, 'label': label} for k, label in keys.items()],
                    }
                    for module, keys in REGISTRY.items()
                ],
            },
        })


def _bundles_by_role_id():
    """role_id -> keys, or None when the table has not been migrated yet.

    The matrix page arriving before `users` 0031/0032 have been applied is a
    real deployment ordering, and a 500 here would read as the feature being
    broken. Answer with what is true instead: roles exist, bundles are empty,
    and the PUT below says why it cannot save.
    """
    try:
        return {
            rp.role_id: list(rp.keys or [])
            for rp in RolePermissions.objects.all()
        }
    except DatabaseError:
        return None


class RolePermissionsListView(APIView):
    """Every active role with its permission bundle — the matrix's columns."""
    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        bundles = _bundles_by_role_id()
        # Distinct holders per role (primary FK ∪ extra_roles), so the page
        # can say why a role cannot be deleted without a second round-trip.
        holders = {}
        for role_id, user_id in User.objects.exclude(role=None).values_list('role_id', 'id'):
            holders.setdefault(role_id, set()).add(user_id)
        for role_id, user_id in User.extra_roles.through.objects.values_list('userrole_id', 'user_id'):
            holders.setdefault(role_id, set()).add(user_id)

        return Response({
            'success': True,
            'data': {
                'migrated': bundles is not None,
                'roles': [
                    {
                        'id': role.id,
                        'name': role.name,
                        'display_name': role.display_name,
                        'is_active': role.is_active,
                        'keys': (bundles or {}).get(role.id, []),
                        'users': len(holders.get(role.id, ())),
                    }
                    for role in UserRole.objects.all().order_by('name')
                ],
            },
        })


class RolePermissionsUpdateView(APIView):
    """Replace one role's bundle. The request body is the whole truth:
    `{"keys": [...]}` — what is listed is granted, what is absent is not.
    Replacement (not merge) because the matrix page shows every box and
    submits every box; a merge would make unticking impossible."""
    permission_classes = [IsAuthenticated, IsAdminRole]

    def put(self, request, role_id):
        try:
            role = UserRole.objects.get(pk=role_id)
        except UserRole.DoesNotExist:
            return Response({'success': False, 'message': 'Role not found'},
                            status=status.HTTP_404_NOT_FOUND)

        keys = request.data.get('keys')
        if not isinstance(keys, list):
            return Response({'success': False, 'message': '`keys` must be a list'},
                            status=status.HTTP_400_BAD_REQUEST)

        cleaned, unknown = [], []
        for key in keys:
            key = str(key).strip()
            if not key or key in cleaned:
                continue
            (cleaned if key in ALL_KEYS else unknown).append(key)
        if unknown:
            return Response(
                {'success': False,
                 'message': f'Unknown permission keys: {", ".join(sorted(unknown))}. '
                            'Keys must exist in core/permission_registry.py.'},
                status=status.HTTP_400_BAD_REQUEST)

        try:
            bundle, _ = RolePermissions.objects.get_or_create(role=role)
            bundle.keys = cleaned
            bundle.save(update_fields=['keys', 'updated_at'])
        except DatabaseError:
            return Response(
                {'success': False,
                 'message': 'Role permission storage is not migrated yet — '
                            'run `python manage.py migrate users` (0031/0032).'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({
            'success': True,
            'message': f'Permissions updated for role {role.display_name}',
            'data': {'role_id': role.id, 'keys': cleaned},
        })


# ---------------------------------------------------------------------------
# Role lifecycle (create / update / delete)
#
# The matrix page is also where roles themselves are managed. Three rules
# keep this safe in a system where role NAMES are still matched by code
# (HasKeyOrRole fallbacks, tracker's ROLE_PAGE_MAP, order-desk scoping):
#
#   * `name` is immutable. Renaming `billing` would silently strip that desk
#     everywhere a fallback still reads names; `display_name` is the safe
#     rename and changes only what humans see.
#   * Privileged roles (core.permissions.PRIVILEGED_ROLE_NAMES) cannot be
#     deleted or deactivated — holding one IS the administrator definition,
#     and a UI slip must not be able to lock every admin out.
#   * Delete only when nobody holds the role. The primary FK enforces this at
#     the database (`on_delete=PROTECT`); extra_roles would silently detach,
#     so it is checked explicitly. A held role is deactivated, not deleted.
# ---------------------------------------------------------------------------

import re as _re

from core.permissions import PRIVILEGED_ROLE_NAMES

_ROLE_NAME_RE = _re.compile(r'^[A-Za-z0-9 _-]{2,50}$')


def _role_holder_count(role):
    """Distinct users holding `role` as primary or extra."""
    primary = set(role.users.values_list('id', flat=True))
    extra = set(role.extra_users.values_list('id', flat=True))
    return len(primary | extra)


class RoleCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request):
        name = str(request.data.get('name') or '').strip()
        display_name = str(request.data.get('display_name') or '').strip() or name

        if not _ROLE_NAME_RE.match(name):
            return Response(
                {'success': False,
                 'message': 'Role name must be 2-50 characters: letters, '
                            'digits, spaces, hyphens or underscores.'},
                status=status.HTTP_400_BAD_REQUEST)
        if UserRole.objects.filter(name__iexact=name).exists():
            return Response(
                {'success': False, 'message': f'A role named "{name}" already exists.'},
                status=status.HTTP_400_BAD_REQUEST)

        role = UserRole.objects.create(name=name, display_name=display_name)
        return Response({
            'success': True,
            'message': f'Role {display_name} created',
            'data': {'id': role.id, 'name': role.name,
                     'display_name': role.display_name, 'is_active': role.is_active},
        }, status=status.HTTP_201_CREATED)


class RoleUpdateView(APIView):
    """Edit `display_name` and `is_active`. `name` is immutable — see the
    block comment above; attempting to change it is a 400 naming the reason,
    not a silent ignore."""
    permission_classes = [IsAuthenticated, IsAdminRole]

    def put(self, request, role_id):
        try:
            role = UserRole.objects.get(pk=role_id)
        except UserRole.DoesNotExist:
            return Response({'success': False, 'message': 'Role not found'},
                            status=status.HTTP_404_NOT_FOUND)

        requested_name = request.data.get('name')
        if requested_name is not None and str(requested_name).strip() != role.name:
            return Response(
                {'success': False,
                 'message': 'Role names cannot be changed: parts of the system '
                            'still match roles by name, so a rename would '
                            'silently change who can do what. Change the '
                            'display name instead.'},
                status=status.HTTP_400_BAD_REQUEST)

        privileged = role.name.strip().lower() in PRIVILEGED_ROLE_NAMES

        if 'display_name' in request.data:
            display_name = str(request.data.get('display_name') or '').strip()
            if not display_name:
                return Response({'success': False, 'message': 'Display name cannot be empty.'},
                                status=status.HTTP_400_BAD_REQUEST)
            role.display_name = display_name

        if 'is_active' in request.data:
            is_active = bool(request.data.get('is_active'))
            if privileged and not is_active:
                return Response(
                    {'success': False,
                     'message': f'The {role.name} role cannot be deactivated.'},
                    status=status.HTTP_400_BAD_REQUEST)
            role.is_active = is_active

        role.save()
        return Response({
            'success': True,
            'message': f'Role {role.display_name} updated',
            'data': {'id': role.id, 'name': role.name,
                     'display_name': role.display_name, 'is_active': role.is_active},
        })


class RoleDeleteView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def delete(self, request, role_id):
        try:
            role = UserRole.objects.get(pk=role_id)
        except UserRole.DoesNotExist:
            return Response({'success': False, 'message': 'Role not found'},
                            status=status.HTTP_404_NOT_FOUND)

        if role.name.strip().lower() in PRIVILEGED_ROLE_NAMES:
            return Response(
                {'success': False,
                 'message': f'The {role.name} role cannot be deleted.'},
                status=status.HTTP_400_BAD_REQUEST)

        holders = _role_holder_count(role)
        if holders:
            return Response(
                {'success': False,
                 'message': f'{holders} user{"s" if holders != 1 else ""} still '
                            f'hold this role. Reassign them first, or '
                            f'deactivate the role instead.'},
                status=status.HTTP_400_BAD_REQUEST)

        display = role.display_name
        role.delete()  # the permission bundle cascades with it
        return Response({'success': True, 'message': f'Role {display} deleted'})
