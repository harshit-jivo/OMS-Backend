"""Admin/config API for the document tracker.

Stage CRUD, lookup CRUD and per-user stage assignment. All gated behind
IsTrackerAdmin. Kept separate from the day-to-day flow views for clarity.
"""
from django.contrib.auth import get_user_model
from django.db.models import ProtectedError
from rest_framework import status as http
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Branch, Category, GstRate, GstType, InvoiceMode, Stage, Unit,
    UserStageAccess,
)
from .permissions import IsTrackerAdmin
from .serializers import (
    BranchSerializer, CategorySerializer, GstRateSerializer, GstTypeSerializer,
    InvoiceModeSerializer, StageSerializer, UnitSerializer,
)

User = get_user_model()

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
