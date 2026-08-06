"""Payments API.

Every endpoint declares `permission_classes` explicitly. The project sets no
DEFAULT_PERMISSION_CLASSES (OMS/settings.py:239), so anything that omits it is
public — which is how 54 endpoints ended up unauthenticated. A money module
must not rely on a default that does not exist.
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Case, IntegerField, Q, When
from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from approvals import services as approval_services
from approvals.permissions import IsApprovalAdmin
from approvals.views import request_context
from attachments import services as attachment_services
from attachments.models import AttachmentType
from attachments.serializers import AttachmentSerializer
from core.pagination import StandardPagination
from core.responses import created, fail, ok
from sap_sync.models import Party as SapParty

from . import services
from .hana_queries import (fetch_open_invoices,
                           fetch_parties_with_open_invoices)
from .permissions import (
    ACTION_PERMISSION_KEYS,
    ACTION_PERMISSION_LABELS,
    CanCreateDeposit,
    CanCreatePayment,
    ReadOrCreateDeposit,
    ReadOrCreatePayment,
    granted_keys,
)
from . import bank_master, hana_queries
from .models import (
    BankDeposit,
    CollectionPerson,
    PaymentMethodEntry,
    PaymentMethodEntry,
    PaymentMethodMapping,
    PaymentReceipt,
    PaymentStatusHistory,
    SapCompanyMap,
    SapPostingHistory,
)
from .serializers import (
    BankDepositCreateSerializer,
    BankDepositSerializer,
    CollectionPersonSerializer,
    PaymentReceiptCreateSerializer,
    PaymentMethodMappingSerializer,
    PaymentReceiptSerializer,
    PaymentStatusHistorySerializer,
    SapCompanyMapSerializer,
    SapPostingHistorySerializer,
)

logger = logging.getLogger(__name__)


def _flag(request, name):
    """Read a boolean query parameter tolerantly.

    Clients differ: the web app sends `true`, some HTTP tools send `1`, and a
    checkbox may send `on`. Accepting all three avoids a filter silently doing
    nothing because of the spelling.
    """
    value = (request.query_params.get(name) or '').strip().lower()
    return value in ('true', '1', 'yes', 'on')


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

    `?with_open_invoices=true` narrows the list to parties that actually owe
    money. The client sends it when the user is recording an invoice payment and
    omits it for an advance — an advance is by definition not invoice-linked, so
    a party with nothing outstanding is still a legitimate payer and must remain
    selectable.
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

        # Live balances, fetched once for the whole company. Only read when the
        # caller asks to filter or wants the figures shown, so the default path
        # stays a pure local-mirror query with no SAP round trip.
        only_open = _flag(request, 'with_open_invoices')
        want_balances = only_open or _flag(request, 'include_balance')
        balances, balance_error = {}, ''
        if want_balances:
            try:
                balances = fetch_parties_with_open_invoices(company=company)
            except DjangoValidationError as exc:
                return fail('; '.join(exc.messages))
            except Exception:
                logger.exception('Open-invoice balance lookup failed for %s',
                                 company)
                if only_open:
                    # Refusing is safer than silently showing every party: the
                    # user asked for "parties that owe money" and would have no
                    # way to tell the filter had quietly stopped working.
                    return fail(
                        'Could not read open balances from SAP right now. '
                        'Try again, or tick "Advance payment" to record a '
                        'payment that is not against an invoice.',
                        status=http_status.HTTP_502_BAD_GATEWAY)
                balance_error = 'Balances unavailable.'

        if only_open:
            parties = parties.filter(card_code__in=list(balances))

        parties = parties.order_by('card_name')
        paginator = StandardPagination()
        page = paginator.paginate_queryset(parties, request, view=self)
        data = []
        for p in page:
            row = {
                'card_code': p.card_code,
                'card_name': p.card_name,
                'label': f'{p.card_name} ({p.card_code})',
                'company': p.category,
                'state': p.state,
            }
            if want_balances and not balance_error:
                stats = balances.get(p.card_code)
                row['open_invoice_count'] = stats['open_count'] if stats else 0
                row['open_balance'] = str(stats['balance_due']) if stats else '0'
            data.append(row)
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
    """Bank accounts for a company, live from SAP House Bank Accounts.

    Replaces the old payment_bank_account table: SAP is the master, so a bank
    added or changed there appears here on the next cache refresh with no code
    or database change.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        if not company:
            return fail('A company is required.')

        banks, meta = bank_master.get_company_banks(
            company, force_refresh=_flag(request, 'refresh'))
        if not meta['available']:
            # Distinct from "SAP has no banks". An empty list would render as
            # a valid empty dropdown instead of saying we could not check.
            return fail(
                'Unable to verify bank information because SAP is currently '
                'unavailable.', errors={'available': False})

        response = ok(banks)
        response.data['meta'] = {'synced_at': meta['synced_at'],
                                 'stale': meta['stale'],
                                 'source': meta['source']}
        return response

class PaymentMethodMappingAdminView(ListCreateAPIView):
    """Admin CRUD for payment method -> SAP account mapping."""

    permission_classes = [IsAuthenticated]
    serializer_class = PaymentMethodMappingSerializer

    def get_queryset(self):
        qs = PaymentMethodMapping.objects.all()
        company = (self.request.query_params.get('company') or '').strip().upper()
        if company:
            qs = qs.filter(company=company)
        return qs


class PaymentMethodMappingAdminDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentMethodMappingSerializer
    queryset = PaymentMethodMapping.objects.all()


class PaymentMethodMappingStatusView(APIView):
    """One row per payment method: what it maps to, and whether SAP still has it.

    Drives the admin page. Returns EVERY method, mapped or not, so a missing
    mapping is as visible as a broken one — an absent row is the commonest
    configuration fault and would otherwise simply not appear.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        if not company:
            return fail('A company is required.')

        resolver = bank_master.PaymentAccountResolver(company)
        banks, meta = bank_master.get_company_banks(
            company, force_refresh=_flag(request, 'refresh'))
        by_key = {b['key'].upper(): b for b in banks}

        stored = {m.payment_method: m for m in PaymentMethodMapping.objects
                  .filter(company=company, is_active=True)}

        rows = []
        for value, label in PaymentMethodEntry.Method.choices:
            if value == PaymentMethodEntry.Method.CASH:
                gl = resolver.cash_gl()
                rows.append({
                    'payment_method': value, 'label': label,
                    'is_cash': True, 'mapping_id': None, 'bank_key': '',
                    'gl_account': gl, 'bank_code': '', 'bank_name': '',
                    'account_number': '', 'branch': '',
                    'configured': bool(gl), 'valid': bool(gl),
                    'error': '' if gl else
                             'No cash G/L account set for this company.',
                })
                continue

            row = stored.get(value)
            bank = by_key.get(row.bank_key.upper()) if row else None
            rows.append({
                'payment_method': value, 'label': label, 'is_cash': False,
                'mapping_id': row.id if row else None,
                'bank_key': row.bank_key if row else '',
                'gl_account': bank['gl_account'] if bank else '',
                'bank_code': bank['bank_code'] if bank else '',
                'bank_name': bank['display_name'] if bank else '',
                'account_number': bank['account_number'] if bank else '',
                'branch': bank['branch'] if bank else '',
                'configured': row is not None,
                'valid': bank is not None,
                'error': ('' if bank is not None else
                          ('Not mapped.' if row is None else
                           f'Mapped account "{row.bank_key}" no longer exists '
                           f'in SAP.')),
            })

        response = ok(rows)
        response.data['meta'] = {
            'synced_at': meta['synced_at'], 'stale': meta['stale'],
            'source': meta['source'], 'available': meta['available'],
            'bank_count': len(banks),
            # One flag the UI can trust for the "Configuration Error" banner.
            'has_errors': any(not r['valid'] for r in rows),
        }
        return response


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

        # Approver-relative views. `status` alone cannot express these: whether
        # a PENDING_APPROVAL receipt is waiting on THIS user depends on which
        # rung it stopped at, which lives on the approval request.
        if view := (request.query_params.get('approval_view') or '').strip():
            ids = approval_services.document_ids_for_view(
                request.user, view, 'PAYMENT', PaymentReceipt)
            if ids is not None:
                qs = qs.filter(id__in=ids)

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

        # "All" groups by what the user must DO about each entry, rather than
        # by date: things needing attention first, finished work last. Within a
        # group the model's own -payment_date ordering still applies.
        if _flag(request, 'group_by_status'):
            qs = qs.annotate(
                _rank=Case(
                    When(status=PaymentReceipt.Status.PENDING_APPROVAL, then=0),
                    When(status__in=[PaymentReceipt.Status.APPROVED,
                                     PaymentReceipt.Status.POSTING_TO_SAP],
                         then=1),
                    When(status__in=[PaymentReceipt.Status.REJECTED,
                                     PaymentReceipt.Status.PENDING_ERROR,
                                     PaymentReceipt.Status.SAP_UNKNOWN],
                         then=2),
                    When(status=PaymentReceipt.Status.POSTED, then=3),
                    default=4,
                    output_field=IntegerField(),
                )
            ).order_by('_rank', '-payment_date', '-id')

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


def _receipt_permissions(receipt, user):
    """What `user` may do with `receipt`. One rule, used by GET and PATCH.

    The client cannot derive these: `can_decide` depends on which rung the
    approval is parked at and whether the user is eligible for it (named
    approvers narrow a level), and self-approval is forbidden.
    """
    approval = receipt.approvals.order_by('-created_at').first()
    can_decide = bool(
        approval is not None
        and approval_services.can_act(user, approval))

    # Who may still change the figures, and when.
    #
    # THE APPROVER HOLDING IT may always edit. That is the point: SAP rejects a
    # document for reasons only visible at posting time — a locked period, a
    # wrong GL, a bad cheque reference — and the person who has to clear it is
    # the one it is parked with. Making them reject the whole entry, wait for
    # the creator, and re-walk the ladder to fix a date would be pure friction.
    # `can_decide` already means "this rung is yours right now", so it is
    # exactly the right test.
    #
    # THE CREATOR may edit only while NO approver has committed:
    #
    #   DRAFT / REJECTED   nobody holds it — it is back with them
    #   PENDING_APPROVAL   only until the FIRST approval lands. After that,
    #                      changing the figures behind an approver's back would
    #                      invalidate a decision already made on the old ones.
    #   PENDING_ERROR      SAP refused, so nothing is committed there
    #
    # A posted document is never editable by anyone: it must match SAP.
    approved_already = bool(
        approval is not None
        and approval.actions.filter(
            action='APPROVE', round_number=approval.round_number).exists())
    creator_may_edit = (
        receipt.created_by_id == user.id
        and (
            receipt.status in (PaymentReceipt.Status.DRAFT,
                               PaymentReceipt.Status.REJECTED,
                               PaymentReceipt.Status.PENDING_ERROR)
            or (receipt.status == PaymentReceipt.Status.PENDING_APPROVAL
                and not approved_already)
        )
    )
    return {
        'can_decide': can_decide,
        'can_edit': (
            not receipt.sap_doc_entry
            and (can_decide or creator_may_edit)
        ),
        # Only the creator resubmits, and only when no chain is open.
        'can_resubmit': (
            receipt.created_by_id == user.id
            and not receipt.sap_doc_entry
            and receipt.status in (PaymentReceipt.Status.DRAFT,
                                   PaymentReceipt.Status.REJECTED,
                                   PaymentReceipt.Status.PENDING_ERROR)
            and not receipt.approvals.filter(
                status__in=['DRAFT', 'PENDING']).exists()
        ),
    }


class PaymentReceiptDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        data = PaymentReceiptSerializer(receipt).data
        data['permissions'] = _receipt_permissions(receipt, request.user)
        return ok(data)

    def patch(self, request, pk):
        """Edit a receipt's content.

        Re-checks `can_edit` here rather than trusting the client: the button is
        hidden when editing is not allowed, but a hidden button is a UI courtesy,
        not a control. This is the only thing standing between a stale app and a
        posted document being rewritten.
        """
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        permissions = _receipt_permissions(receipt, request.user)
        if not permissions['can_edit']:
            reason = (
                f'{receipt.receipt_no} is already posted to SAP and cannot be '
                f'changed.'
                if receipt.sap_doc_entry else
                f'{receipt.receipt_no} can no longer be edited — it has been '
                f'approved or is not yours to change.'
            )
            return fail(reason, status=http_status.HTTP_403_FORBIDDEN)

        serializer = PaymentReceiptCreateSerializer(
            receipt, data=request.data, partial=True,
            context={'request': request})
        if not serializer.is_valid():
            return fail('Could not update the receipt.',
                        errors=serializer.errors)
        try:
            receipt = serializer.save()
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))

        data = PaymentReceiptSerializer(receipt).data
        data['permissions'] = _receipt_permissions(receipt, request.user)
        return ok(data, message='Receipt updated.')


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
        .select_related('deposited_by', 'created_by')
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
    """Posted CASH/CHEQUE receipts in this company not yet banked."""

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
            # A bank deposit is somebody physically carrying money to a branch,
            # so only physical instruments can be in one. UPI settles straight
            # into the bank account and has nothing to hand over — offering it
            # here invited a deposit for money that was never held.
            .filter(methods__method__in=[
                PaymentMethodEntry.Method.CASH,
                PaymentMethodEntry.Method.CHEQUE,
            ])
            # A receipt with BOTH cash and cheque lines matches the join twice.
            .distinct()
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
