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
