"""Payments API.

Every endpoint declares `permission_classes` explicitly. The project sets no
DEFAULT_PERMISSION_CLASSES (OMS/settings.py:239), so anything that omits it is
public — which is how 54 endpoints ended up unauthenticated. A money module
must not rely on a default that does not exist.
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from approvals.permissions import IsApprovalAdmin
from approvals.views import request_context
from attachments import services as attachment_services
from attachments.models import AttachmentType
from attachments.serializers import AttachmentSerializer
from core.pagination import StandardPagination
from core.responses import created, fail, ok
from sap_sync.models import Party as SapParty

from . import services
from .hana_queries import fetch_open_invoices
from .permissions import (
    ACTION_PERMISSION_KEYS,
    ACTION_PERMISSION_LABELS,
    CanCreateDeposit,
    CanCreatePayment,
    ReadOrCreateDeposit,
    ReadOrCreatePayment,
    granted_keys,
)
from .models import (
    BankAccount,
    BankDeposit,
    CollectionPerson,
    PaymentReceipt,
    PaymentStatusHistory,
    SapCompanyMap,
    SapPostingHistory,
)
from .serializers import (
    BankAccountSerializer,
    BankDepositCreateSerializer,
    BankDepositSerializer,
    CollectionPersonSerializer,
    PaymentReceiptCreateSerializer,
    PaymentReceiptSerializer,
    PaymentStatusHistorySerializer,
    SapCompanyMapSerializer,
    SapPostingHistorySerializer,
)

logger = logging.getLogger(__name__)


def user_companies(user):
    """Companies this user may transact in — every configured company.

    Previously narrowed by UserPartyAssignment. That is an ORDERS concept (a
    salesperson's territory) and does not apply here: a collection agent handles
    whoever pays. Deriving company access from it also meant a user with no
    assignments silently saw a different list from one with them.

    Access to the payments module is governed by the action permissions
    (Payments_Create, Deposit_Approve, ...) — not by which parties someone sells
    to.
    """
    return SapCompanyMap.objects.filter(is_active=True)


# ---------------------------------------------------------------------------
# Cascade: company -> parties -> open invoices
# ---------------------------------------------------------------------------

class CompanyListView(APIView):
    """Step 1. Companies (SAP categories) available to the caller."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = user_companies(request.user).order_by('sort_order', 'company')
        return ok(SapCompanyMapSerializer(rows, many=True).data)


class PartyListView(APIView):
    """Step 2. Parties for a company. EVERY party, not a per-user subset.

    Deliberately NOT scoped by UserPartyAssignment, unlike PartyView in the
    orders flow (orders/views.py:1984-2023). Those assignments exist so a
    salesperson only sells to their own accounts — a collection agent has no
    such territory: they receive money from whoever pays, and scoping them to an
    assignment list would leave them unable to record a legitimate payment.

    The company filter IS still mandatory: `card_code` is not unique across
    categories (sap_sync/models.py:58), so the same code is a different business
    partner per company and an unscoped lookup can credit the wrong customer.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        if not company:
            return fail('A company is required.',
                        errors={'company': 'This query parameter is required.'})

        parties = SapParty.objects.filter(category__iexact=company)

        if search := (request.query_params.get('search') or '').strip():
            parties = parties.filter(
                Q(card_name__icontains=search) | Q(card_code__istartswith=search))

        parties = parties.order_by('card_name')
        paginator = StandardPagination()
        page = paginator.paginate_queryset(parties, request, view=self)
        data = [
            {
                'card_code': p.card_code,
                'card_name': p.card_name,
                'label': f'{p.card_name} ({p.card_code})',
                'company': p.category,
                'state': p.state,
            }
            for p in page
        ]
        return paginator.get_paginated_response(data)


class OpenInvoiceListView(APIView):
    """Step 3. LIVE open invoices for a party.

    Read live, never mirrored: a balance changes every time anyone takes a
    payment, and a stale one would let two collectors over-apply against the
    same invoice.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        card_code = (request.query_params.get('card_code') or '').strip()
        if not company or not card_code:
            return fail('company and card_code are required.')

        # Authorise the PAIR server-side — never trust that the client walked
        # the cascade to get here. The check is that the party exists in this
        # company, NOT that the user is assigned to it: a collection agent takes
        # money from whoever pays, so an assignment list would block legitimate
        # collections. See PartyListView.
        if not SapParty.objects.filter(
            card_code=card_code, category__iexact=company
        ).exists():
            return fail(f'No party "{card_code}" exists in {company}.',
                        status=http_status.HTTP_404_NOT_FOUND)

        try:
            rows = fetch_open_invoices(
                company=company,
                card_code=card_code,
                search=(request.query_params.get('search') or '').strip(),
                limit=int(request.query_params.get('limit') or 100),
                offset=int(request.query_params.get('offset') or 0),
            )
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))
        except Exception:
            logger.exception('Open-invoice query failed for %s/%s', company, card_code)
            return fail('Could not read open invoices from SAP right now.',
                        status=http_status.HTTP_502_BAD_GATEWAY)

        return ok({'results': rows, 'count': len(rows)})


class CollectionPersonListView(APIView):
    """The manually-maintained "Received From" people."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = CollectionPerson.objects.filter(is_active=True)
        if company := (request.query_params.get('company') or '').strip().upper():
            rows = rows.filter(Q(company=company) | Q(company=''))
        return ok(CollectionPersonSerializer(rows.order_by('name'), many=True).data)


class BankAccountListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = BankAccount.objects.filter(is_active=True)
        if company := (request.query_params.get('company') or '').strip().upper():
            rows = rows.filter(company=company)
        return ok(BankAccountSerializer(rows.order_by('name'), many=True).data)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def _receipt_queryset(user):
    """Receipts visible to `user` — every configured company.

    No per-user narrowing: what someone may DO is decided by the action
    permissions, and a caller who wants only their own entries asks for them
    with `?mine=true`. Restricting reads by party assignment would hide a
    receipt from the very approver who has to decide on it.
    """
    return (
        PaymentReceipt.objects
        .select_related('received_from_person', 'created_by')
        .prefetch_related('methods__denominations', 'allocations',
                          'attachments', 'approvals')
    )


class PaymentReceiptListCreateView(APIView):
    # Anyone signed in may READ the queue (an approver has to see what they are
    # approving); raising a new receipt requires Payments_Create.
    permission_classes = [IsAuthenticated, ReadOrCreatePayment]

    def get(self, request):
        qs = _receipt_queryset(request.user)
        if value := request.query_params.get('status'):
            # Comma-separated so the client can express one UI option that
            # spans several stored statuses (e.g. "Completed" = POSTED).
            qs = qs.filter(status__in=[s.strip().upper()
                                       for s in value.split(',') if s.strip()])
        if value := request.query_params.get('company'):
            qs = qs.filter(company=value.upper())
        if value := request.query_params.get('card_code'):
            qs = qs.filter(card_code=value)
        # Inclusive date window over payment_date, for the tracking screens.
        if value := request.query_params.get('date_from'):
            qs = qs.filter(payment_date__gte=value)
        if value := request.query_params.get('date_to'):
            qs = qs.filter(payment_date__lte=value)
        if request.query_params.get('mine') == 'true':
            qs = qs.filter(created_by=request.user)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            PaymentReceiptSerializer(page, many=True).data)

    def post(self, request):
        serializer = PaymentReceiptCreateSerializer(
            data=request.data, context={'request': request})
        if not serializer.is_valid():
            return fail('Could not create the receipt.', errors=serializer.errors)
        try:
            receipt = serializer.save()
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))
        return created(PaymentReceiptSerializer(receipt).data,
                       message='Receipt created.')


class PaymentReceiptDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        return ok(PaymentReceiptSerializer(receipt).data)


class PaymentReceiptSubmitView(APIView):
    """Send a draft (or rejected) receipt into the approval chain."""

    # Submitting is part of raising the document, so it takes the same grant as
    # creating one — otherwise a user could create drafts they can never submit.
    permission_classes = [IsAuthenticated, CanCreatePayment]

    def post(self, request, pk):
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        try:
            services.submit_receipt(receipt, request.user, ctx=request_context(request))
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))
        receipt.refresh_from_db()
        return ok(PaymentReceiptSerializer(receipt).data,
                  message='Submitted for approval.')


class PaymentReceiptHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from django.contrib.contenttypes.models import ContentType

        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        rows = PaymentStatusHistory.objects.filter(
            content_type=ContentType.objects.get_for_model(PaymentReceipt),
            object_id=receipt.pk,
        )
        return ok(PaymentStatusHistorySerializer(rows, many=True).data)


class PaymentReceiptSapHistoryView(APIView):
    """Every SAP posting attempt on one receipt, newest first.

    Read-only by construction: there is no POST/PATCH/DELETE here, and the
    serializer marks every field read_only. History is only ever appended, by
    the posting service.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        rows = (SapPostingHistory.objects
                .filter(payment=receipt)
                .select_related('created_by')
                .order_by('-created_at', '-id'))
        return ok(SapPostingHistorySerializer(rows, many=True).data)


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

def _deposit_queryset(user):
    """Deposits visible to `user` — see _receipt_queryset."""
    return (
        BankDeposit.objects
        .select_related('bank_account', 'deposited_by', 'created_by')
        .prefetch_related('lines__receipt', 'attachments', 'approvals')
    )


class BankDepositListCreateView(APIView):
    # Read for any authenticated user; creating requires Deposit_Create.
    permission_classes = [IsAuthenticated, ReadOrCreateDeposit]

    def get(self, request):
        qs = _deposit_queryset(request.user)
        if value := request.query_params.get('status'):
            # Comma-separated — see PaymentReceiptListCreateView.
            qs = qs.filter(status__in=[s.strip().upper()
                                       for s in value.split(',') if s.strip()])
        if value := request.query_params.get('company'):
            qs = qs.filter(company=value.upper())
        if value := request.query_params.get('date_from'):
            qs = qs.filter(deposit_date__gte=value)
        if value := request.query_params.get('date_to'):
            qs = qs.filter(deposit_date__lte=value)
        if request.query_params.get('mine') == 'true':
            qs = qs.filter(created_by=request.user)

        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            BankDepositSerializer(page, many=True).data)

    def post(self, request):
        serializer = BankDepositCreateSerializer(
            data=request.data, context={'request': request})
        if not serializer.is_valid():
            return fail('Could not create the deposit.', errors=serializer.errors)
        try:
            deposit = serializer.save()
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))
        return created(BankDepositSerializer(deposit).data, message='Deposit created.')


class BankDepositDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        deposit = get_object_or_404(_deposit_queryset(request.user), pk=pk)
        return ok(BankDepositSerializer(deposit).data)


class BankDepositSubmitView(APIView):
    # Same grant as creating — see PaymentReceiptSubmitView.
    permission_classes = [IsAuthenticated, CanCreateDeposit]

    def post(self, request, pk):
        deposit = get_object_or_404(_deposit_queryset(request.user), pk=pk)
        try:
            services.submit_deposit(deposit, request.user, ctx=request_context(request))
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))
        deposit.refresh_from_db()
        return ok(BankDepositSerializer(deposit).data,
                  message='Submitted for approval.')


class DepositableReceiptListView(APIView):
    """Posted receipts in this company not yet banked."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        if not company:
            return fail('A company is required.')
        qs = (
            _receipt_queryset(request.user)
            # "Not yet banked" is the absence of a BankDepositLine, which is the
            # only link between a receipt and a deposit and carries the unique
            # constraint that stops a receipt being banked twice.
            .filter(company=company,
                    status=PaymentReceipt.Status.POSTED,
                    deposit_lines__isnull=True)
            .order_by('-payment_date')
        )
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(
            PaymentReceiptSerializer(page, many=True).data)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

class _AttachmentUploadBase(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)      # NOTE: plural — the
    # singular `parser_class` in legal/views.py:14 is a typo and is inert.

    model = None
    allowed_types = ()

    def get_document(self, request, pk):
        raise NotImplementedError

    def post(self, request, pk):
        document = self.get_document(request, pk)
        upload = request.FILES.get('file')
        if not upload:
            return fail('No file was uploaded.',
                        errors={'file': 'This field is required.'})

        attachment_type = (request.data.get('attachment_type') or '').strip().upper()
        if attachment_type not in self.allowed_types:
            return fail(
                f'attachment_type must be one of: {", ".join(self.allowed_types)}.')

        try:
            attachment = attachment_services.attach(
                document=document, upload=upload,
                attachment_type=attachment_type, user=request.user)
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))
        except Exception:
            logger.exception('Attachment upload failed for %s %s',
                             self.model.__name__, pk)
            return fail('The file store is currently unavailable.',
                        status=http_status.HTTP_502_BAD_GATEWAY)

        return created(AttachmentSerializer(attachment).data, message='File uploaded.')


class ReceiptAttachmentUploadView(_AttachmentUploadBase):
    model = PaymentReceipt
    allowed_types = (AttachmentType.CHEQUE_IMAGE, AttachmentType.UPI_SCREENSHOT)

    def get_document(self, request, pk):
        return get_object_or_404(_receipt_queryset(request.user), pk=pk)


class DepositAttachmentUploadView(_AttachmentUploadBase):
    model = BankDeposit
    allowed_types = (AttachmentType.DEPOSIT_SLIP, AttachmentType.DEPOSIT_RECEIPT)

    def get_document(self, request, pk):
        return get_object_or_404(_deposit_queryset(request.user), pk=pk)


class BankAccountAdminListCreateView(ListCreateAPIView):
    """Admin CRUD for deposit target accounts.

    Distinct from BankAccountListView above: that one is the picker feed and
    returns only ACTIVE rows, while an admin has to see and reactivate the
    inactive ones too.
    """

    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = BankAccountSerializer

    def get_queryset(self):
        qs = BankAccount.objects.all()
        if company := (self.request.query_params.get('company') or '').strip().upper():
            qs = qs.filter(company=company)
        return qs.order_by('company', 'name')


class BankAccountAdminDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = BankAccountSerializer
    queryset = BankAccount.objects.all()


class CollectionPersonAdminListCreateView(ListCreateAPIView):
    """Admin CRUD for the "Received From" people.

    A person belongs to one company (or to all, when left blank) so the mobile
    dropdown can be scoped — the same collector name can exist separately under
    OIL and BEVERAGES.
    """

    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = CollectionPersonSerializer

    def get_queryset(self):
        qs = CollectionPerson.objects.all()
        if company := (self.request.query_params.get('company') or '').strip().upper():
            # Company-specific rows PLUS the all-companies ones, matching what
            # the picker feed returns.
            qs = qs.filter(Q(company=company) | Q(company=''))
        return qs.order_by('company', 'name')


class CollectionPersonAdminDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = CollectionPersonSerializer
    queryset = CollectionPerson.objects.all()


class CompanyMappingListCreateView(ListCreateAPIView):
    """Admin CRUD for company -> SAP database mappings.

    Previously only reachable through Django admin, which meant an admin had to
    leave the app to make the payments module usable at all — nothing works
    until these rows exist.
    """

    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = SapCompanyMapSerializer
    queryset = SapCompanyMap.objects.all().order_by('sort_order', 'company')


class CompanyMappingDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsApprovalAdmin]
    serializer_class = SapCompanyMapSerializer
    queryset = SapCompanyMap.objects.all()


class MyPaymentPermissionsView(APIView):
    """What the CALLER may do in this module.

    The clients need this to hide buttons they must not offer. Returning it from
    the server means the rule lives in exactly one place — a client that
    hardcoded its own copy would drift the moment the rule changed, and an admin
    (who holds every key implicitly) would be handled differently in each app.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        keys = granted_keys(request.user)
        return ok({
            'permissions': sorted(keys),
            'available': [
                {'key': k, 'label': ACTION_PERMISSION_LABELS[k]}
                for k in ACTION_PERMISSION_KEYS
            ],
            'can': {k: (k in keys) for k in ACTION_PERMISSION_KEYS},
        })
