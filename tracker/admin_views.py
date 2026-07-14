"""Admin/config API for the document tracker.

Stage CRUD, lookup CRUD and per-user stage assignment. All gated behind
IsTrackerAdmin. Kept separate from the day-to-day flow views for clarity.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db.models import ProtectedError
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import UserRole

from .models import (
    Branch, Category, GstRate, GstType, InvoiceMode, Stage, Unit,
    UserStageAccess,
)
from .permissions import IsTrackerAdmin, ROLE_PAGE_MAP
from .serializers import (
    BranchSerializer, CategorySerializer, GstRateSerializer, GstTypeSerializer,
    InvoiceModeSerializer, StageSerializer, UnitSerializer,
)

User = get_user_model()

# The tracker sub-roles a tracker admin is allowed to create / manage.
TRACKER_ROLE_NAMES = set(ROLE_PAGE_MAP.keys())

# kind -> (model, serializer) for the generic lookup endpoints.
LOOKUP_MAP = {
    'categories': (Category, CategorySerializer),
    'units': (Unit, UnitSerializer),
    'branches': (Branch, BranchSerializer),
    'modes': (InvoiceMode, InvoiceModeSerializer),
    'gst_types': (GstType, GstTypeSerializer),
    'gst_rates': (GstRate, GstRateSerializer),
}


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
class StageAdminListCreate(APIView):
    permission_classes = [IsTrackerAdmin]

    def get(self, request):
        # All stages, including inactive, for admin management.
        return Response(StageSerializer(Stage.objects.all(), many=True).data)

    def post(self, request):
        serializer = StageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=http.HTTP_201_CREATED)


class StageAdminDetail(APIView):
    permission_classes = [IsTrackerAdmin]

    def _get(self, pk):
        return Stage.objects.filter(pk=pk).first()

    def patch(self, request, pk):
        stage = self._get(pk)
        if not stage:
            return Response(status=http.HTTP_404_NOT_FOUND)
        serializer = StageSerializer(stage, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, pk):
        stage = self._get(pk)
        if not stage:
            return Response(status=http.HTTP_404_NOT_FOUND)
        try:
            stage.delete()
        except ProtectedError:
            return Response(
                {'detail': 'Stage is in use by invoices; deactivate it instead.'},
                status=http.HTTP_400_BAD_REQUEST)
        return Response(status=http.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Lookups (generic by kind)
# ---------------------------------------------------------------------------
def _resolve(kind):
    return LOOKUP_MAP.get(kind, (None, None))


class LookupAdminListCreate(APIView):
    permission_classes = [IsTrackerAdmin]

    def get(self, request, kind):
        model, serializer_cls = _resolve(kind)
        if not model:
            return Response({'detail': 'Unknown lookup kind.'}, status=http.HTTP_404_NOT_FOUND)
        return Response(serializer_cls(model.objects.all(), many=True).data)

    def post(self, request, kind):
        model, serializer_cls = _resolve(kind)
        if not model:
            return Response({'detail': 'Unknown lookup kind.'}, status=http.HTTP_404_NOT_FOUND)
        serializer = serializer_cls(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=http.HTTP_201_CREATED)


class LookupAdminDetail(APIView):
    permission_classes = [IsTrackerAdmin]

    def patch(self, request, kind, pk):
        model, serializer_cls = _resolve(kind)
        if not model:
            return Response({'detail': 'Unknown lookup kind.'}, status=http.HTTP_404_NOT_FOUND)
        obj = model.objects.filter(pk=pk).first()
        if not obj:
            return Response(status=http.HTTP_404_NOT_FOUND)
        serializer = serializer_cls(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, kind, pk):
        model, _ = _resolve(kind)
        if not model:
            return Response({'detail': 'Unknown lookup kind.'}, status=http.HTTP_404_NOT_FOUND)
        obj = model.objects.filter(pk=pk).first()
        if not obj:
            return Response(status=http.HTTP_404_NOT_FOUND)
        try:
            obj.delete()
        except ProtectedError:
            return Response(
                {'detail': 'Value is in use by invoices; deactivate it instead.'},
                status=http.HTTP_400_BAD_REQUEST)
        return Response(status=http.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# User <-> stage assignment
# ---------------------------------------------------------------------------
class UserStageListView(APIView):
    """List users with the stage ids they're currently assigned to."""
    permission_classes = [IsTrackerAdmin]

    def get(self, request):
        access = UserStageAccess.objects.filter(is_active=True)
        by_user = {}
        for a in access:
            by_user.setdefault(a.user_id, []).append(a.stage_id)

        users = User.objects.filter(is_active=True).select_related('role')
        search = request.query_params.get('search')
        if search:
            users = users.filter(username__icontains=search)

        data = [{
            'id': u.id,
            'username': u.username,
            'name': getattr(u, 'name', '') or u.username,
            'role': getattr(getattr(u, 'role', None), 'name', None),
            'stage_ids': sorted(by_user.get(u.id, [])),
        } for u in users.order_by('username')[:500]]
        return Response(data)


# ---------------------------------------------------------------------------
# Tracker user management (create / list / delete) — TRACKER users only.
# A tracker admin can only ever touch users who hold a tracker sub-role, never
# other OMS users.
# ---------------------------------------------------------------------------
def _serialize_tracker_user(u):
    return {
        'id': u.id,
        'username': u.username,
        'name': getattr(u, 'name', '') or u.username,
        'email': u.email or '',
        'phone': getattr(u, 'phone', '') or '',
        'role': getattr(getattr(u, 'role', None), 'name', None),
        'role_display': getattr(getattr(u, 'role', None), 'display_name', None),
        'is_active': u.is_active,
    }


class TrackerUserListCreate(APIView):
    """List all tracker users, or create a new one under a tracker sub-role."""
    permission_classes = [IsTrackerAdmin]

    def get(self, request):
        users = (
            User.objects.filter(role__name__in=TRACKER_ROLE_NAMES)
            .select_related('role').order_by('username')
        )
        return Response([_serialize_tracker_user(u) for u in users])

    def post(self, request):
        data = request.data
        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        role_name = (data.get('role') or '').strip().lower()
        name = (data.get('name') or '').strip() or username

        if not username or not password:
            return Response({'detail': 'Username and password are required.'},
                            status=http.HTTP_400_BAD_REQUEST)
        if role_name not in TRACKER_ROLE_NAMES:
            return Response(
                {'detail': f'Role must be one of {sorted(TRACKER_ROLE_NAMES)}.'},
                status=http.HTTP_400_BAD_REQUEST)
        if User.objects.filter(username__iexact=username).exists():
            return Response({'detail': 'A user with that username already exists.'},
                            status=http.HTTP_400_BAD_REQUEST)

        role = UserRole.objects.filter(name=role_name).first()
        if not role:
            return Response({'detail': 'Tracker role not found; seed the roles first.'},
                            status=http.HTTP_400_BAD_REQUEST)

        user = User(
            username=username, name=name,
            email=(data.get('email') or '').strip() or None,
            phone=(data.get('phone') or '').strip() or None,
            role=role, is_active=True, is_staff=False, is_superuser=False,
            created_by=request.user,
        )
        user.set_password(password)
        try:
            user.save()
        except IntegrityError:
            return Response({'detail': 'Could not create user (duplicate?).'},
                            status=http.HTTP_400_BAD_REQUEST)
        return Response(_serialize_tracker_user(user), status=http.HTTP_201_CREATED)


class TrackerUserDetail(APIView):
    """Edit or delete a tracker user. Refuses to touch non-tracker users."""
    permission_classes = [IsTrackerAdmin]

    def _get_tracker_user(self, user_id):
        user = User.objects.filter(pk=user_id).select_related('role').first()
        if not user:
            return None, Response(status=http.HTTP_404_NOT_FOUND)
        role_name = getattr(getattr(user, 'role', None), 'name', None)
        if role_name not in TRACKER_ROLE_NAMES:
            return None, Response(
                {'detail': 'Only tracker users can be managed here.'},
                status=http.HTTP_403_FORBIDDEN)
        return user, None

    def patch(self, request, user_id):
        user, err = self._get_tracker_user(user_id)
        if err:
            return err
        data = request.data

        if 'name' in data:
            user.name = (data.get('name') or '').strip() or user.username
        if 'email' in data:
            user.email = (data.get('email') or '').strip() or None
        if 'phone' in data:
            user.phone = (data.get('phone') or '').strip() or None
        if 'is_active' in data:
            user.is_active = bool(data.get('is_active'))
        if data.get('role'):
            role_name = str(data['role']).strip().lower()
            if role_name not in TRACKER_ROLE_NAMES:
                return Response(
                    {'detail': f'Role must be one of {sorted(TRACKER_ROLE_NAMES)}.'},
                    status=http.HTTP_400_BAD_REQUEST)
            role = UserRole.objects.filter(name=role_name).first()
            if not role:
                return Response({'detail': 'Tracker role not found.'},
                                status=http.HTTP_400_BAD_REQUEST)
            user.role = role
        # Optional password reset — only when a non-empty value is sent.
        if (data.get('password') or '').strip():
            user.set_password(data['password'])

        user.updated_by = request.user
        user.save()
        return Response(_serialize_tracker_user(user))

    def delete(self, request, user_id):
        user, err = self._get_tracker_user(user_id)
        if err:
            return err
        try:
            user.delete()
        except ProtectedError:
            # The user is referenced by invoices they created — keep the record
            # but deactivate so they can no longer log in.
            user.is_active = False
            user.save(update_fields=['is_active'])
            return Response(
                {'detail': 'User has tracker history and was deactivated instead '
                           'of deleted.', 'deactivated': True},
                status=http.HTTP_200_OK)
        return Response(status=http.HTTP_204_NO_CONTENT)


class UserStageSetView(APIView):
    """Replace a user's full set of stage assignments with `stage_ids`."""
    permission_classes = [IsTrackerAdmin]

    def put(self, request, user_id):
        user = User.objects.filter(pk=user_id).first()
        if not user:
            return Response(status=http.HTTP_404_NOT_FOUND)
        stage_ids = set(request.data.get('stage_ids') or [])
        valid_ids = set(Stage.objects.filter(id__in=stage_ids).values_list('id', flat=True))
        if stage_ids - valid_ids:
            return Response({'detail': 'One or more stage ids are invalid.'},
                            status=http.HTTP_400_BAD_REQUEST)

        existing = {
            a.stage_id: a for a in UserStageAccess.objects.filter(user=user)
        }
        # Activate / create wanted, deactivate the rest.
        for sid in valid_ids:
            row = existing.get(sid)
            if row:
                if not row.is_active:
                    row.is_active = True
                    row.assigned_by = request.user
                    row.save(update_fields=['is_active', 'assigned_by'])
            else:
                UserStageAccess.objects.create(
                    user=user, stage_id=sid, assigned_by=request.user)
        for sid, row in existing.items():
            if sid not in valid_ids and row.is_active:
                row.is_active = False
                row.save(update_fields=['is_active'])

        return Response({'user': user.id, 'stage_ids': sorted(valid_ids)})
