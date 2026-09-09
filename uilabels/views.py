"""UI-label endpoints.

Two audiences:

* Every authenticated client (web + mobile) reads the flat public label map
  once after login: ``GET /api/ui-config/labels/`` → ``{"price_list": "..."}``.
* Admins manage labels through a small CRUD surface under
  ``/api/ui-config/admin/labels/``.

Follows the house style: plain ``APIView``s returning a
``{success, message, data}`` envelope for the admin surface. The public map is
returned bare so clients can use it directly as a dictionary.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import UILabel
from .permissions import IsAdminRole
from .serializers import UILabelSerializer


class PublicLabelsView(APIView):
    """Flat ``{field_key: display_name}`` map of every ACTIVE label.

    This is the single source of truth both frontends fetch once after login
    and cache. Kept deliberately tiny and unwrapped for cheap client use.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        labels = UILabel.objects.filter(is_active=True).values_list(
            'field_key', 'display_name')
        return Response(dict(labels))


class PublicFieldsView(APIView):
    """Field-behaviour config for input fields (as opposed to plain labels).

    Returns, for EVERY row, its label plus the two behaviour flags clients need
    to render an input dynamically::

        {"po_number": {"label": "PO Number", "enabled": true, "required": false}}

    IMPORTANT: unlike ``/labels/``, this endpoint must include INACTIVE rows.
    An inactive row means the admin turned the whole entry off, which for a
    field is the same intent as "hide it". If inactive rows were omitted the
    client would fall back to its built-in default (enabled) and the field would
    reappear — the opposite of what the admin wanted. So the effective
    ``enabled`` folds in ``is_active``: a row that is inactive OR not enabled is
    reported as ``enabled: false``; ``required`` is likewise false unless the
    field is actually shown.

    Kept separate from the flat ``/labels/`` map so that endpoint stays a simple
    string→string dictionary.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = UILabel.objects.all().values(
            'field_key', 'display_name', 'is_active', 'is_enabled',
            'is_required')
        data = {}
        for row in rows:
            enabled = bool(row['is_active']) and bool(row['is_enabled'])
            data[row['field_key']] = {
                'label': row['display_name'],
                'enabled': enabled,
                # A hidden field can never be required.
                'required': enabled and bool(row['is_required']),
            }
        return Response(data)


class AdminLabelListCreateView(APIView):
    """List every label (active or not) and create new ones. Admin only."""
    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        labels = UILabel.objects.all()
        return Response({
            "success": True,
            "message": "UI labels fetched.",
            "data": UILabelSerializer(labels, many=True).data,
        })

    def post(self, request):
        serializer = UILabelSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"success": False, "message": "Invalid label data.",
                 "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer.save()
        return Response(
            {"success": True, "message": "UI label created.",
             "data": serializer.data},
            status=status.HTTP_201_CREATED,
        )


class AdminLabelDetailView(APIView):
    """Retrieve / update / delete a single label. Admin only."""
    permission_classes = [IsAuthenticated, IsAdminRole]

    def _get(self, pk):
        return UILabel.objects.filter(pk=pk).first()

    def get(self, request, pk):
        label = self._get(pk)
        if not label:
            return Response(
                {"success": False, "message": "UI label not found."},
                status=status.HTTP_404_NOT_FOUND)
        return Response({
            "success": True, "message": "UI label fetched.",
            "data": UILabelSerializer(label).data,
        })

    def put(self, request, pk):
        label = self._get(pk)
        if not label:
            return Response(
                {"success": False, "message": "UI label not found."},
                status=status.HTTP_404_NOT_FOUND)
        serializer = UILabelSerializer(label, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {"success": False, "message": "Invalid label data.",
                 "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer.save()
        return Response({
            "success": True, "message": "UI label updated.",
            "data": serializer.data,
        })

    def delete(self, request, pk):
        label = self._get(pk)
        if not label:
            return Response(
                {"success": False, "message": "UI label not found."},
                status=status.HTTP_404_NOT_FOUND)
        label.delete()
        return Response(
            {"success": True, "message": "UI label deleted."},
            status=status.HTTP_200_OK,
        )
