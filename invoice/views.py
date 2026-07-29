import json
import logging

import pymssql
import requests

from django.conf import settings
from django.db import transaction, IntegrityError
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from .models import InvoiceLog , InvocieHistory , InvoiceRefLogs , CreditLimitLogs
from .serializers import InvoiceLogSerializer ,InvoiveHistorySerializer , InvoiceRefLogsSerializer , CreditLimitLogSerializer
from rest_framework.permissions import IsAuthenticated , AllowAny
from rest_framework import status
from rest_framework import generics
from rest_framework.generics import CreateAPIView, ListAPIView
from django.http import HttpResponse

from hana.services.services import SalesOrderService
from .services.jsap_db import get_credit_flow_id
from .services.fg_stock import CONTEXT_KEY as FG_STOCK_CONTEXT_KEY, build_fg_stock_map

logger = logging.getLogger(__name__)



class InvoiceLogCreateView(APIView):
    def post(self, request):
        # A resubmit from the "Edit" action on a rejected invoice carries the id of
        # the log it replaces. It is not a model field, so keep it out of the
        # serializer and act on it only once the replacement has been saved.
        data = request.data.copy()
        edited_from = data.pop('edited_from', None)
        if isinstance(edited_from, (list, tuple)):
            edited_from = edited_from[0] if edited_from else None

        serializer = InvoiceLogSerializer(data=data)
        if serializer.is_valid():
            with transaction.atomic():
                invoice_log_instance = serializer.save(created_by=request.user)
                InvocieHistory.objects.create(
                    invoice_log=invoice_log_instance,
                    so_number=invoice_log_instance.so_number,
                    party_name=invoice_log_instance.party_name,
                    total_amount=invoice_log_instance.total_amount,
                    status=invoice_log_instance.status,
                    rejection_reason=invoice_log_instance.rejection_reason,
                    error_message=invoice_log_instance.error_message,
                    invoice_payload=invoice_log_instance.invoice_payload,
                    created_by=request.user
                )
                source = self._close_edited_source(edited_from, request.user)
                if source is not None:
                    # Link the replacement to the version it grew out of, so the
                    # approver of this log can see (and trace) the rejection
                    # behind it.
                    invoice_log_instance.supersedes = source
                    invoice_log_instance.save(update_fields=['supersedes'])
            return Response(
                InvoiceLogSerializer(invoice_log_instance).data,
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def _close_edited_source(self, edited_from, user):
        """Retire the rejected log this submission replaces, and return it.

        Only runs once the replacement exists, so an edit that is started and then
        abandoned leaves the original REJECTED and visible to reviewers. The
        rejection_reason is deliberately left on the row — it is the record of why
        the invoice was reworked. Returns None when there is nothing to retire, in
        which case the new log carries no lineage.
        """
        if edited_from in (None, ''):
            return None
        try:
            source = InvoiceLog.objects.select_for_update().get(pk=edited_from)
        except (InvoiceLog.DoesNotExist, ValueError, TypeError):
            return None
        # Only a rejected invoice can be reworked; anything else (approved, already
        # posted to SAP) must not be moved by a resubmission.
        if source.status != 'REJECTED':
            return None

        source.status = 'EDITED'
        source.save()
        InvocieHistory.objects.create(
            invoice_log=source,
            so_number=source.so_number,
            party_name=source.party_name,
            total_amount=source.total_amount,
            status=source.status,
            rejection_reason=source.rejection_reason,
            error_message=source.error_message,
            invoice_payload=source.invoice_payload,
            created_by=user,
        )
        return source


class InvoicelogStatusUpdateView(APIView):

    def patch(self, request, pk):
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        user = request.data.get('user')
        
        if new_status not in dict(InvoiceLog.STATUS_CHOICES):
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)
        if new_status == 'REJECTED' and not request.data.get('rejection_reason'):
            return Response({'error': 'Rejection reason is required when status is REJECTED'}, status=status.HTTP_400_BAD_REQUEST)
        if new_status == 'REJECTED':
            invoice_log.rejection_reason = request.data.get('rejection_reason')
        # Record a supplied error message whatever the target status. A failed SAP
        # repost is worth logging even when the status does not become ERROR — a
        # credit-limit rejection on an invoice that already has a request in
        # flight stays CL_RAISED, but the reviewer still needs to see what SAP
        # said on the latest attempt.
        if request.data.get('error_message'):
            invoice_log.error_message = request.data.get('error_message')

        invoice_log.status = new_status
        InvocieHistory.objects.create(
                    invoice_log=invoice_log,
                    so_number=invoice_log.so_number,
                    party_name=invoice_log.party_name,
                    total_amount=invoice_log.total_amount,
                    status=invoice_log.status,
                    rejection_reason=invoice_log.rejection_reason,
                    error_message=invoice_log.error_message,
                    invoice_payload=invoice_log.invoice_payload,
                    created_by=user
        )
        print(f"Creator{invoice_log.created_by}")
        print(f"Approver{request.user}")
        invoice_log.save()
        return Response({'message': 'Status updated successfully'}, status=status.HTTP_200_OK)
    
    
class InvoiceLogListView(APIView):
    
  def get(self, request):
    inv_status = request.query_params.get('status')
    warehouse = request.query_params.get('whs')
    if not warehouse:
        return Response(
            {'error': 'Warehouse Code is a required parameter.'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    invoice_logs = (
        InvoiceLog.objects
        .select_related('supersedes')
        .prefetch_related('superseded_by')
        .filter(warehouse=warehouse)
    )

    if inv_status:
        invoice_logs = invoice_logs.filter(status=inv_status)

    # Resolve the queryset once so the FG stock lookup and the serializer work
    # off the same rows (one HANA query for the whole page, not one per line).
    invoice_logs = list(invoice_logs)
    serializer = InvoiceLogSerializer(
        invoice_logs,
        many=True,
        context={FG_STOCK_CONTEXT_KEY: build_fg_stock_map(invoice_logs)},
    )
    return Response(serializer.data)

class InvoiceHistoryView(APIView):

    def get(self ,  request , pk):
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        # A reworked invoice is a chain of logs, not one row. Return the history of
        # every earlier version too, so the reviewer sees one continuous timeline
        # ending at this log instead of a stump that starts after the rejection.
        chain_ids = [log.pk for log in invoice_log.revision_chain()]
        history = (
            InvocieHistory.objects
            .filter(invoice_log_id__in=chain_ids)
            .order_by('created_at', 'id')
        )
        serializer = InvoiveHistorySerializer(history, many=True)
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


class UpdateInvoiceLogView(generics.RetrieveUpdateAPIView):
    queryset = InvoiceLog.objects.all()
    serializer_class = InvoiceLogSerializer
    lookup_field = 'id'

    def perform_update(self, serializer):
        invoice_log_instance = serializer.save()

        InvocieHistory.objects.create(
            invoice_log=invoice_log_instance,
            so_number=invoice_log_instance.so_number,
            party_name=invoice_log_instance.party_name,
            total_amount=invoice_log_instance.total_amount,
            status=invoice_log_instance.status,
            rejection_reason=invoice_log_instance.rejection_reason,
            error_message=invoice_log_instance.error_message,
            invoice_payload=invoice_log_instance.invoice_payload,
            created_by=self.request.user
        )


class CreditLimitCardsView(APIView):

    def get(self, request):
        company = request.query_params.get('company', '1')
        url = f"{settings.DSR_API_BASE}/api/CreditLimit/GetCustomerCards"
        try:
            dsr_response = requests.get(url, params={'company': company}, timeout=20, verify=False)
            try:
                body = dsr_response.json()
            except ValueError:
                body = {'error': dsr_response.text}
            return Response(body, status=dsr_response.status_code)
        except requests.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class CreditLimitRequestView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        document_data = request.data.get('documentData')
        attachment = request.FILES.get('attachment')

        invoice_log_id = request.data.get('invoice_log_id')
        if not document_data or not attachment:
            return Response(
                {'error': 'documentData and attachment are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not invoice_log_id:
            return Response(
                {'error': 'invoice_log_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            invoice_log = InvoiceLog.objects.get(id=invoice_log_id)
        except InvoiceLog.DoesNotExist:
            return Response(
                {'error': f'No invoice log found with id {invoice_log_id}.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # credit_limit_logs is keyed by invoice_log_id (one request per invoice).
        # Check BEFORE calling DSR — otherwise a duplicate attempt creates a second
        # CL document in JSAP and only then fails on the insert.
        existing = CreditLimitLogs.objects.filter(invoice_log_id=invoice_log.id).first()
        if existing:
            return Response(
                {
                    'error': 'A credit-limit request has already been raised for this invoice.',
                    'detail': (
                        f'Credit-limit document #{existing.jsap_doc_id} was raised for this '
                        f'invoice on {existing.created_at:%d %b %Y %H:%M}. '
                        'Track that request instead of raising a new one.'
                    ),
                    'jsap_doc_id': existing.jsap_doc_id,
                    'created_at': existing.created_at,
                    'invoice_log_id': invoice_log.id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            parsed = json.loads(document_data)
            parsed['createdBy'] = settings.OMS_JSAP_USER_ID
            document_data = json.dumps(parsed)
        except (ValueError, TypeError):
            return Response(
                {'error': 'documentData must be a valid JSON object'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        url = f"{settings.DSR_API_BASE}/api/CreditLimit/CreateCLDocumentV2"
        try:
            dsr_response = requests.post(
                url,
                data={'documentData': document_data},
                files={'attachment': (attachment.name, attachment, attachment.content_type)},
                timeout=30,
                verify=False,
            )
            try:
                body = dsr_response.json()
            except ValueError:
                body = {'message': dsr_response.text}

            credit_document_id = body.get("creditDocumentId") if isinstance(body, dict) else None
            if credit_document_id is None:
                logger.error("CL request for invoice %s returned no creditDocumentId: %s", invoice_log.id, body)
                return Response(
                    {
                        'error': 'The credit-limit service did not return a document id.',
                        'details': body,
                    },
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            try:
                CreditLimitLogs.objects.create(
                    invoice_log=invoice_log,
                    jsap_doc_id=credit_document_id,
                    party_name=invoice_log.party_name,
                    created_by=request.user,
                )
            except IntegrityError:
                # Lost a race with a concurrent request. The CL document exists in
                # JSAP either way, so report it as the same conflict rather than a 500.
                logger.warning("Duplicate credit-limit request for invoice %s", invoice_log.id)
                return Response(
                    {
                        'error': 'A credit-limit request has already been raised for this invoice.',
                        'jsap_doc_id': credit_document_id,
                        'invoice_log_id': invoice_log.id,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            return Response(body, status=dsr_response.status_code)
        except requests.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)



class GetCreditLimitJSAPFlow(APIView):
    def get(self, request):
        invoice_id = request.query_params.get('invoice_id')
        # `company` is still accepted (callers send it) but no longer used: the
        # flow is now found by document id, which is unique across companies.
        if not invoice_id:
            return Response(
                {'error': 'invoice_id is a required parameter.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        jsap_doc = CreditLimitLogs.objects.filter(invoice_log_id=invoice_id).first()
  
        if not jsap_doc:
            return Response(
                {'error': 'No CreditLimitLogs entry found for the given invoice_id.'}, 
                status=status.HTTP_404_NOT_FOUND
            )

        doc_id = jsap_doc.jsap_doc_id

        # Resolve the flow id straight from the JSAP database. The previous route
        # — POST GetAllDocuments for the *current* month and scan it for this
        # document — silently failed for any request raised in an earlier month,
        # which is exactly when a reviewer wants to check a pending approval.
        try:
            flow_id = get_credit_flow_id(doc_id)
        except (ConnectionError, pymssql.Error) as exc:
            logger.exception("JSAP flow lookup failed for credit document %s", doc_id)
            return Response(
                {'error': 'Unable to reach the JSAP database', 'details': str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if flow_id is None:
            return Response(
                {'error': 'No approval flow has been created for this credit-limit request yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        flow_url = f"{settings.DSR_API_BASE}/api/CreditLimit/GetApprovalFlow/"

        try:
            flow_response = requests.get(
                flow_url,
                params={'flowId': flow_id},
                timeout=20,
                verify=False
            )


            if not flow_response.ok:
                return Response(
                    {'error': 'Failed to fetch flow details', 'details': flow_response.text}, 
                    status=flow_response.status_code
                )

            flow_body = flow_response.json()
            
            return Response(flow_body, status=status.HTTP_200_OK)

        except requests.exceptions.Timeout:
            return Response({'error': 'JSAP API timeout'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except ValueError:
            return Response({'error':'Invalid JSON received from JSAP API'}, status=status.HTTP_502_BAD_GATEWAY)
        
class GetPrintReport(APIView):
    def get(self, request):
        docNum = request.query_params.get('docNum')
        if not docNum:
            return Response({"error": "docNum is required"}, status=status.HTTP_400_BAD_REQUEST)

        docEntry = SalesOrderService().get_docEntry(docNum)
        if not docEntry:
            return Response({"error": f"No invoice found for docNum {docNum}"},
                            status=status.HTTP_404_NOT_FOUND)

        url = f"{settings.CRYSTAL_URL}/api/billprint/{docEntry[0]['DocEntry']}"
        try:
            crystal_response = requests.get(url, timeout=60, verify=False)
        except requests.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        if not crystal_response.ok:
            return Response({'error': 'Failed to generate print report',
                             'details': crystal_response.text},
                            status=crystal_response.status_code)

        resp = HttpResponse(
            crystal_response.content,
            status=crystal_response.status_code,
            content_type=crystal_response.headers.get('Content-Type', 'application/pdf'),
        )
        resp['Content-Disposition'] = f'inline; filename="invoice_{docNum}.pdf"'
       
        resp.xframe_options_exempt = True
        return resp

  
class InvoiceLogListwoWhsView(APIView):
    
  def get(self, request):
    inv_status = request.query_params.get('status')
    # select_related/prefetch_related keep the lineage fields on the serializer
    # from costing a query per row.
    invoice_logs = InvoiceLog.objects.select_related('supersedes').prefetch_related('superseded_by')
    if inv_status:
        invoice_logs = invoice_logs.filter(status=inv_status)

    invoice_logs = list(invoice_logs)
    serializer = InvoiceLogSerializer(
        invoice_logs,
        many=True,
        context={FG_STOCK_CONTEXT_KEY: build_fg_stock_map(invoice_logs)},
    )
    return Response(serializer.data)