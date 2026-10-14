"""A sales order's customer PO: attached on Add Sales, shown to every desk.

The file itself goes through the shared `attachments` app — the same type and
size checks and permission-checked download as payment images — linked to the
order generically as a PURCHASE_ORDER attachment. It is stored locally under
MEDIA_ROOT (settings.ORDER_PO_ATTACHMENTS), not on the payments share or SAP.

Reading follows `Order.can_be_viewed_by` (the creator, admins and the
reviewing desks). Adding or removing is for the order's creator and admins,
and only while the order can still be edited: once it is closed or booked
into SAP, its PO is part of the record.
"""
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from attachments import services as attachment_services
from attachments.models import AttachmentType
from attachments.serializers import AttachmentSerializer
from attachments.storage import validate_upload
from core.permissions import is_admin
from orders.models import Order
from orders.views.lifecycle import _edit_locked_error

#: More than this is almost certainly the wrong files.
MAX_PO_FILES = 5


def _po_attachments(order):
    return attachment_services.for_document(order).filter(
        attachment_type=AttachmentType.PURCHASE_ORDER)


def _manage_refusal(order, user):
    """Why `user` may not add or remove this order's PO, or None."""
    if not (order.created_by_id == user.id or is_admin(user)):
        return Response({'error': "Only the order's creator can change its PO."},
                        status=status.HTTP_403_FORBIDDEN)
    locked = _edit_locked_error(order)
    if locked:
        return Response({'error': locked}, status=status.HTTP_409_CONFLICT)
    return None


class OrderAttachmentsView(APIView):
    """GET the order's PO files; POST one or more as multipart `file`."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)
        if not order.can_be_viewed_by(request.user):
            return Response({'error': 'You do not have permission to view this order.'},
                            status=status.HTTP_403_FORBIDDEN)
        return Response(AttachmentSerializer(_po_attachments(order), many=True).data)

    def post(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)
        refused = _manage_refusal(order, request.user)
        if refused:
            return refused

        uploads = request.FILES.getlist('file')
        if not uploads:
            return Response({'error': 'Choose a PO file to attach.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if _po_attachments(order).count() + len(uploads) > MAX_PO_FILES:
            return Response({'error': f'An order can have at most {MAX_PO_FILES} PO files.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Every file is checked before any is stored, so a bad second file
        # cannot leave the first one attached on its own.
        try:
            for upload in uploads:
                validate_upload(upload)
            created = [
                attachment_services.attach(
                    document=order, upload=upload,
                    attachment_type=AttachmentType.PURCHASE_ORDER, user=request.user)
                for upload in uploads
            ]
        except ValidationError as exc:
            return Response({'error': ' '.join(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response(AttachmentSerializer(created, many=True).data,
                        status=status.HTTP_201_CREATED)


class OrderAttachmentDetailView(APIView):
    """DELETE one PO file from the order."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, order_id, attachment_id):
        order = get_object_or_404(Order, id=order_id)
        refused = _manage_refusal(order, request.user)
        if refused:
            return refused
        attachment = get_object_or_404(_po_attachments(order), id=attachment_id)
        attachment_services.detach(attachment)
        return Response(status=status.HTTP_204_NO_CONTENT)

