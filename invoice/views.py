"""Sales-invoice review/approval screen, credit-limit requests, bill printing.

Phase 2.4 audit: two views (`UsedSalesOrdersView`, `ReservedBatchesView`)
already declared `permission_classes = [AllowAny]`; every other view
relied silently on the project-wide default. All of them are now explicit,
matching the two that already were. None is gated with
`core.permissions.IsAdminRole`: create/approve/reject/delete here are ordinary
reviewer actions on the billing desk, not org-wide admin functions — the same
distinction `sap_sync` draws for its own (deliberately non-admin-gated) order
approval views. `branches_for_user` below is the one place this app's access
logic genuinely needed `core.permissions`: it used to check `is_superuser`
alone for "sees every branch", the exact bug class `core.permissions.is_admin`
exists to close (an admin via role/`extra_roles`/`is_staff` wasn't recognised).
"""
import json
import logging

import pymssql
import requests

from django.conf import settings
from django.db import transaction, IntegrityError
from django.shortcuts import render
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from .models import InvoiceLog , InvocieHistory , InvoiceRefLogs , CreditLimitLogs
from .serializers import InvoiceLogSerializer ,InvoiveHistorySerializer , InvoiceRefLogsSerializer , CreditLimitLogSerializer
from rest_framework.permissions import IsAuthenticated , AllowAny
from rest_framework import status
from rest_framework import generics
from rest_framework.generics import CreateAPIView, ListAPIView

from core import crystal
from core.permissions import is_admin
from hana.services.services import SalesOrderService
from hana.utils import normalize_branch, resolve_doc_entry
from .services.jsap_db import get_credit_flow_id
from .services.fg_stock import CONTEXT_KEY as FG_STOCK_CONTEXT_KEY, build_fg_stock_map
from .services.item_names import CONTEXT_KEY as ITEM_NAME_CONTEXT_KEY, build_item_name_map


def _external_verify():
    """TLS verification for the DSR and Crystal calls below.

    These call sites passed `verify=_external_verify()` as a literal. Both services are
    plain http today, where requests ignores `verify` entirely — so it was not
    a live exposure, it was a TRAP: the day either URL gains an `s`, the calls
    would keep working and silently stop verifying anything, and nothing in the
    code would say so.

    Defaults to on. A self-signed certificate on either box is then an explicit
    decision (`EXTERNAL_SSL_VERIFY=false`) rather than a default nobody chose.
    """
    return getattr(settings, 'EXTERNAL_SSL_VERIFY', True)

logger = logging.getLogger(__name__)


def _wants_deleted(request):
    """True when the caller explicitly asked to see soft-deleted entries.

    Off by default, so every existing caller of the list endpoints keeps getting
    only live rows without changing anything.
    """
    return str(request.query_params.get('include_deleted', '')).lower() in ('1', 'true', 'yes')


# The two company databases an invoice can belong to, keyed by the product
# category a user is assigned. MART lives in the oil company alongside OIL,
# which is the same split `resolve_company_db_for_order` makes.
CATEGORY_BRANCHES = {
    'OIL': 'OIL',
    'MART': 'OIL',
    'BEVERAGES': 'BEVERAGE',
}


def branches_for_user(user):
    """Which invoice branches this user may see, or None for "all of them".

    An OIL user has no business reviewing beverage bills and vice versa, so the
    review screens are scoped to the branches behind the user's own categories.

    None — meaning unrestricted — is returned for admins and for anyone with no
    category assigned at all. That last case matters: admins and auditors are
    set up without categories, and scoping them to nothing would empty the
    review screen for the very people who have to work it.

    Phase 2.4: this used to check `is_superuser` alone, which is exactly the
    bug `core.permissions` was built to close (see its module docstring) — an
    admin holding the role via `extra_roles`, or via `is_staff`, was not
    recognised here and could have been scoped down to their own categories
    instead of seeing the whole review screen. `core.permissions.is_admin`
    covers all three; this is a widening, not a new restriction.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    if is_admin(user):
        return None

    names = {
        str(name or '').strip().upper()
        for name in user.categories.values_list('category', flat=True)
    }
    primary = getattr(getattr(user, 'category', None), 'category', '')
    if primary:
        names.add(str(primary).strip().upper())

    branches = {CATEGORY_BRANCHES[name] for name in names if name in CATEGORY_BRANCHES}
    return branches or None


def _history_actor(request):
    """Who to record on an `InvocieHistory` row, as a username string.

    `InvocieHistory.created_by` is a CharField, and every other writer in this
    module passes `request.user` straight into it — Django coerces the instance
    with `str()`, which `AbstractUser.__str__` defines as the username. This
    returns that same string, so a row written by the status-update path is
    attributed identically to one written by create, edit, delete or restore.

    Why it exists: that path read the actor out of the request BODY
    (`request.data.get('user')`), and no client has ever sent that key — the
    review screen PATCHes `{status, rejection_reason}` and nothing more
    (`invoiceReview/helpers.ts:updateInvoiceStatus`). So every approval,
    rejection, SAP post and credit-limit row landed with `created_by` NULL,
    while the paths using `request.user` produced none. Measured on live: 192
    POSTED_TO_SAP, 162 ERROR, 9 APPROVED and 6 CL_RAISED rows with no actor on
    them. Those 369 are not recoverable — `audit.AuditLog` does not cover
    `/api/invoice/` paths (`audit/pages.py:_PATH_RULES`), so nothing else in
    the system recorded who made those decisions.

    The body value survives as a FALLBACK, never as the first choice. These
    views are `AllowAny`, so an unauthenticated caller really can reach them,
    and a name such a caller declares about itself is worth keeping as a hint
    while being worth nothing as proof — which is exactly why it must not
    override an authenticated identity. `None` when there is neither, so
    "nobody knows" stays distinguishable from a real name instead of becoming
    the literal string 'AnonymousUser'.
    """
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user.get_username()
    claimed = str(request.data.get('user') or '').strip()
    return claimed[:125] or None


def scope_logs_to_user(invoice_logs, request):
    """Narrow an InvoiceLog queryset to the branches the caller may see."""
    branches = branches_for_user(getattr(request, 'user', None))
    if branches is None:
        return invoice_logs
    return invoice_logs.filter(branch__in=branches)



class InvoiceLogCreateView(APIView):
    permission_classes = [AllowAny]

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
                    created_by=request.user,
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
        # posted to SAP, or removed from the review screen) must not be moved by a
        # resubmission.
        if source.status != 'REJECTED' or source.is_deleted:
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
    permission_classes = [AllowAny]

    def patch(self, request, pk):
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        # A deleted entry is off the review screen; it must not be approvable,
        # rejectable or postable to SAP until someone restores it.
        if invoice_log.is_deleted:
            return Response(
                {'error': 'This invoice has been deleted. Restore it before changing its status.'},
                status=status.HTTP_409_CONFLICT,
            )

        new_status = request.data.get('status')

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

        # SAP identifiers of the document that was just created. Sent by the
        # review screen alongside POSTED_TO_SAP so the row can offer a bill
        # print without anyone having to look the number up in SAP first.
        if request.data.get('sap_doc_num'):
            invoice_log.sap_doc_num = str(request.data.get('sap_doc_num'))[:50]
        if request.data.get('sap_doc_entry'):
            invoice_log.sap_doc_entry = str(request.data.get('sap_doc_entry'))[:50]

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
                    created_by=_history_actor(request),
        )
        invoice_log.save()
        return Response({'message': 'Status updated successfully'}, status=status.HTTP_200_OK)
    
    
class InvoiceLogDeleteView(APIView):
    """Soft-delete a review entry, and restore one.

    Only the statuses in ``InvoiceLog.DELETABLE_STATUSES`` may be removed —
    an APPROVED or POSTED_TO_SAP log corresponds to a decision already acted on
    (a real SAP document, in the posted case), so it stays on the screen.

    Nothing is erased: the row is stamped and hidden, its history is untouched,
    and a DELETED entry is appended to the timeline so the removal is itself
    part of the audit trail.
    """

    permission_classes = [AllowAny]

    def delete(self, request, pk):
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        # Idempotent: a double-click or a retry on a row the client has already
        # dropped from its list must not read as a failure.
        if invoice_log.is_deleted:
            return Response(
                {'message': 'Invoice already deleted', 'id': invoice_log.pk},
                status=status.HTTP_200_OK,
            )

        if invoice_log.status not in InvoiceLog.DELETABLE_STATUSES:
            return Response(
                {
                    'error': f'An invoice marked "{invoice_log.get_status_display()}" cannot be deleted.',
                    'detail': (
                        'Only '
                        + ', '.join(InvoiceLog.DELETABLE_STATUSES)
                        + ' entries can be removed from the review screen.'
                    ),
                    'status': invoice_log.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        reason = (request.data.get('delete_reason') or '').strip() or None

        with transaction.atomic():
            invoice_log.is_deleted = True
            invoice_log.deleted_at = timezone.now()
            invoice_log.deleted_by = request.user
            invoice_log.delete_reason = reason
            invoice_log.save(
                update_fields=['is_deleted', 'deleted_at', 'deleted_by', 'delete_reason']
            )
            # Status on the log itself is left alone — the entry was PENDING or
            # ERROR when it was removed and that is what the record should say.
            # The history row carries DELETED so the timeline shows the removal,
            # with the reason in the same field a rejection reason is archived in.
            InvocieHistory.objects.create(
                invoice_log=invoice_log,
                so_number=invoice_log.so_number,
                party_name=invoice_log.party_name,
                total_amount=invoice_log.total_amount,
                status='DELETED',
                rejection_reason=reason,
                error_message=invoice_log.error_message,
                invoice_payload=invoice_log.invoice_payload,
                created_by=request.user,
            )

        return Response(
            {
                'message': 'Invoice deleted successfully',
                'id': invoice_log.pk,
                'deleted_at': invoice_log.deleted_at,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, pk):
        """Restore a soft-deleted entry — the undo for a mistaken delete."""
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        if not invoice_log.is_deleted:
            return Response(
                {'message': 'Invoice is not deleted', 'id': invoice_log.pk},
                status=status.HTTP_200_OK,
            )

        with transaction.atomic():
            invoice_log.is_deleted = False
            invoice_log.deleted_at = None
            invoice_log.deleted_by = None
            invoice_log.delete_reason = None
            invoice_log.save(
                update_fields=['is_deleted', 'deleted_at', 'deleted_by', 'delete_reason']
            )
            InvocieHistory.objects.create(
                invoice_log=invoice_log,
                so_number=invoice_log.so_number,
                party_name=invoice_log.party_name,
                total_amount=invoice_log.total_amount,
                status='RESTORED',
                rejection_reason=invoice_log.rejection_reason,
                error_message=invoice_log.error_message,
                invoice_payload=invoice_log.invoice_payload,
                created_by=request.user,
            )

        return Response(
            {'message': 'Invoice restored successfully', 'id': invoice_log.pk},
            status=status.HTTP_200_OK,
        )


class InvoiceLogListView(APIView):
  permission_classes = [AllowAny]

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

    # An OIL user sees oil bills, a beverage user sees beverage bills.
    invoice_logs = scope_logs_to_user(invoice_logs, request)

    # Soft-deleted entries are off the review screen unless asked for by name.
    if not _wants_deleted(request):
        invoice_logs = invoice_logs.filter(is_deleted=False)

    if inv_status:
        invoice_logs = invoice_logs.filter(status=inv_status)

    # Resolve the queryset once so the FG stock lookup and the serializer work
    # off the same rows (one HANA query for the whole page, not one per line).
    invoice_logs = list(invoice_logs)
    serializer = InvoiceLogSerializer(
        invoice_logs,
        many=True,
        context={
            FG_STOCK_CONTEXT_KEY: build_fg_stock_map(invoice_logs),
            ITEM_NAME_CONTEXT_KEY: build_item_name_map(invoice_logs),
        },
    )
    return Response(serializer.data)

class InvoiceHistoryView(APIView):
    permission_classes = [AllowAny]

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
    permission_classes = [AllowAny]

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
    permission_classes = [AllowAny]
    # Deleted entries are excluded rather than merely hidden: editing one would
    # write a history entry against a log nobody can see.
    queryset = InvoiceLog.objects.filter(is_deleted=False)
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
            created_by=self.request.user,
        )


class CreditLimitCardsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        company = request.query_params.get('company', '1')
        url = f"{settings.DSR_API_BASE}/api/CreditLimit/GetCustomerCards"
        try:
            dsr_response = requests.get(url, params={'company': company}, timeout=20, verify=_external_verify())
            try:
                body = dsr_response.json()
            except ValueError:
                body = {'error': dsr_response.text}
            return Response(body, status=dsr_response.status_code)
        except requests.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class CreditLimitRequestView(APIView):
    permission_classes = [AllowAny]
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

        # Raising a credit-limit document in JSAP for an invoice that has been
        # removed from the review screen would leave a real approval request
        # pointing at nothing.
        if invoice_log.is_deleted:
            return Response(
                {'error': 'This invoice has been deleted. Restore it before raising a credit-limit request.'},
                status=status.HTTP_409_CONFLICT,
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
                verify=_external_verify(),
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
    permission_classes = [AllowAny]

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
                verify=_external_verify()
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
    permission_classes = [AllowAny]

    # Each company is rendered through its own path on the Crystal service,
    # which maps it to that company's ODBC DSN and HANA schema.
    # 'api/billprint/{DocEntry}' (no company) is the service's legacy OIL route.
    _CRYSTAL_PATHS = {
        'OIL': 'api/billprint',
        'BEVERAGE': 'api/billprint/bev',
        'MART': 'api/billprint/mart',
    }

    def get(self, request):
        docNum = request.query_params.get('docNum')
        # Which company's invoice this is: it picks both the schema the DocNum
        # is resolved against and the Crystal path the PDF is rendered from.
        # Defaults to OIL so existing callers keep working unchanged.
        branch = normalize_branch(request.query_params.get('branch'))
        if branch is None or branch not in self._CRYSTAL_PATHS:
            return Response(
                {'error': 'branch must be one of: '
                          + ', '.join(sorted(self._CRYSTAL_PATHS))},
                status=status.HTTP_400_BAD_REQUEST)

        # The caller may already know the internal OINV key (the review screen
        # keeps it from the SAP post response). Using it skips the DocNum ->
        # DocEntry lookup, which is what the Crystal service wants anyway.
        doc_entry = (request.query_params.get('docEntry') or '').strip()

        if not doc_entry:
            if not docNum:
                return Response({"error": "docNum is required"}, status=status.HTTP_400_BAD_REQUEST)

            doc_entry = resolve_doc_entry(docNum, branch)
            if not doc_entry:
                return Response({"error": f"No {branch} invoice found for docNum {docNum}"},
                                status=status.HTTP_404_NOT_FOUND)

        # The fetch, the "<DocNum> <Party Name>.pdf" naming and the inline
        # Content-Disposition are shared with orders/crystal/ — see core/crystal.py.
        return crystal.render_pdf(
            self._CRYSTAL_PATHS[branch],
            doc_entry,
            crystal.download_name(docNum or doc_entry,
                                  request.query_params.get('party')),
            ascii_fallback='invoice.pdf',
        )

  
class InvoiceLogListwoWhsView(APIView):
  permission_classes = [AllowAny]

  def get(self, request):
    inv_status = request.query_params.get('status')
    # select_related/prefetch_related keep the lineage fields on the serializer
    # from costing a query per row.
    invoice_logs = InvoiceLog.objects.select_related('supersedes').prefetch_related('superseded_by')
    invoice_logs = scope_logs_to_user(invoice_logs, request)
    if not _wants_deleted(request):
        invoice_logs = invoice_logs.filter(is_deleted=False)
    if inv_status:
        invoice_logs = invoice_logs.filter(status=inv_status)

    invoice_logs = list(invoice_logs)
    serializer = InvoiceLogSerializer(
        invoice_logs,
        many=True,
        context={
            FG_STOCK_CONTEXT_KEY: build_fg_stock_map(invoice_logs),
            ITEM_NAME_CONTEXT_KEY: build_item_name_map(invoice_logs),
        },
    )
    return Response(serializer.data)

class UsedSalesOrdersView(APIView):
    """Which sales orders already appear on an invoice log.

    The Sales Invoice screen lists a customer's open SOs from SAP, which has no
    idea an OMS invoice is already in flight against one — SAP only closes the
    order once the invoice actually posts, so a second user can pick the same SO
    and invoice it twice. This is what lets the picker mark those SOs.

    `so_number` holds the comma-joined DocNums of every line on the log, so it
    is split back out here rather than matched as a string.

    Rejected and soft-deleted logs are left out: neither blocks re-invoicing, so
    flagging them would train people to ignore the badge.
    """

    permission_classes = [AllowAny]

    BLOCKING_STATUSES = ('PENDING', 'APPROVED', 'EDITED', 'ERROR', 'CL_RAISED', 'POSTED_TO_SAP')

    def get(self, request):
        logs = (
            InvoiceLog.objects
            .filter(is_deleted=False, status__in=self.BLOCKING_STATUSES)
            .order_by('-created_at')
            .values('id', 'so_number', 'status', 'sap_doc_num', 'created_at')
        )

        card_code = (request.query_params.get('card_code') or '').strip()
        branch = (request.query_params.get('branch') or '').strip()
        if branch:
            logs = logs.filter(branch=branch)

        used = {}
        for log in logs:
            for raw in str(log['so_number'] or '').split(','):
                so_number = raw.strip()
                if not so_number or so_number in used:
                    continue
                # Ordered newest first, so the first row wins — the most recent
                # attempt is the one worth showing.
                used[so_number] = {
                    'so_number': so_number,
                    'log_id': log['id'],
                    'status': log['status'],
                    'sap_doc_num': log['sap_doc_num'] or '',
                    'created_at': log['created_at'],
                }

        return Response({
            'success': True,
            'card_code': card_code,
            'data': list(used.values()),
            'total': len(used),
        })


class ReservedBatchesView(APIView):
    """How much of each batch an in-flight invoice log has already committed.

    SAP does not know a batch is spoken for until the invoice actually posts, so
    the batch picker keeps offering that stock and two drafts happily allocate
    the same pieces. This covers exactly that window.

    A QUANTITY per batch, not a flag: one batch holds thousands of pieces and is
    normally split across many invoices, so the caller subtracts what is held
    and keeps the rest usable. Treating a batch as taken outright meant twenty
    pieces on someone else's draft locked the whole batch.

    Keyed by item and warehouse as well as batch number: the same batch number
    can exist for a different item, and holding it everywhere would block stock
    nothing has claimed.

    A REJECTED log releases its batches — that invoice is not going to post, so
    its stock is free again. Soft-deleted logs release theirs for the same
    reason.
    """

    permission_classes = [AllowAny]

    # Statuses that have NOT reached SAP. POSTED_TO_SAP is deliberately absent:
    # once the invoice posts, SAP has already taken the stock out of the batch,
    # so OIBT reports the reduced quantity. Holding it here as well would
    # subtract the same pieces twice and make a batch look emptier than it is --
    # harmless when a hold merely hid the batch, wrong now that the quantity is
    # netted off. This endpoint exists only for the window before SAP knows.
    HOLDING_STATUSES = ('PENDING', 'APPROVED', 'EDITED', 'ERROR', 'CL_RAISED')

    def get(self, request):
        logs = (
            InvoiceLog.objects
            .filter(is_deleted=False, status__in=self.HOLDING_STATUSES)
            .order_by('-created_at')
        )

        branch = normalize_branch(request.query_params.get('branch'), default=None)
        if branch:
            logs = logs.filter(branch=branch)

        # Scoped like the review screens, so a beverage user is not blocked by
        # an oil draft they cannot even see.
        logs = scope_logs_to_user(logs, request)

        reserved = {}
        for log in logs.values('id', 'status', 'invoice_payload'):
            for line in ((log['invoice_payload'] or {}).get('DocumentLines') or []):
                if not isinstance(line, dict):
                    continue
                item_code = str(line.get('ItemCode') or '').strip().upper()
                warehouse = str(line.get('WarehouseCode') or '').strip().upper()
                for batch in (line.get('BatchNumbers') or []):
                    if not isinstance(batch, dict):
                        continue
                    number = str(batch.get('BatchNumber') or '').strip()
                    if not (item_code and number):
                        continue
                    key = (item_code, warehouse, number.upper())
                    entry = reserved.get(key)
                    if entry is None:
                        entry = {
                            'item_code': item_code,
                            'warehouse_code': warehouse,
                            'batch_number': number,
                            'quantity': 0.0,
                            'log_id': log['id'],
                            'status': log['status'],
                        }
                        reserved[key] = entry
                    try:
                        entry['quantity'] += float(batch.get('Quantity') or 0)
                    except (TypeError, ValueError):
                        pass

        return Response({
            'success': True,
            'data': list(reserved.values()),
            'total': len(reserved),
        })
