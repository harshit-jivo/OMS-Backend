from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import InvoiceLog , InvoiceRefLogs
from .serializers import InvoiceLogSerializer , InvoiceRefLogsSerializer
from rest_framework.permissions import IsAuthenticated , AllowAny
from rest_framework import status
from rest_framework.generics import CreateAPIView, ListAPIView

from hana.services.services import SalesOrderService


class InvoiceLogCreateView(APIView):
    def post(self, request):
        serializer = InvoiceLogSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    
class InvoiceLogListView(APIView):
    
    def get(self , request):
        status = request.query_params.get('status')
        if status:
            invoice_logs = InvoiceLog.objects.filter(status=status)
        else:
            invoice_logs = InvoiceLog.objects.all()

        serializer = InvoiceLogSerializer(invoice_logs, many=True)
        return Response(serializer.data)



class InvoiceRefLogCreateView(CreateAPIView):

    serializer_class = InvoiceRefLogsSerializer
    queryset = InvoiceRefLogs.objects.all()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        fields = dict(serializer.validated_data)
        ref_id = fields.pop('ref_id')

        # SAP's Service Layer sometimes returns "matching record not found" even
        # though the invoice draft was actually created. Each draft is stamped
        # with our ref number in ODRF."U_OMS_REF", so verify against HANA before
        # trusting that error: if a draft with this ref exists, it was created.
        verification_error = None
        draft_exists = False
        try:
            draft_exists = bool(SalesOrderService().get_draft_verfication(ref_id))
        except Exception as exc:
            # Verification unavailable (e.g. HANA unreachable) - log the attempt
            # as submitted and let the caller know it could not be confirmed.
            verification_error = str(exc)

        if verification_error is not None:
            message = "Invoice ref logged, but HANA verification is currently unavailable."
        elif draft_exists:
            # Draft really is in SAP - record success regardless of any
            # "matching record not found" error the client may have seen.
            fields['status'] = 'SUCCESS'
            fields['error_message'] = None
            message = "Invoice created"
        else:
            message = "No matching draft found in HANA for this ref number."

        # Each ref number maps to exactly one invoice draft, so keep the log
        # idempotent: a repeat submit (double-click / retry / StrictMode) updates
        # the existing row instead of inserting a duplicate.
        instance, created = InvoiceRefLogs.objects.update_or_create(
            ref_id=ref_id,
            defaults=fields,
        )

        return Response(
            {
                "message": message,
                "verified": draft_exists,
                "duplicate": not created,
                "verification_error": verification_error,
                "data": self.get_serializer(instance).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )