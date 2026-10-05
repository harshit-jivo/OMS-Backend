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
from django.utils.dateparse import parse_date
from rest_framework import status as http_status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import permissions as payment_permissions
from . import workflow_flow
from attachments import services as attachment_services
from attachments.models import AttachmentType
from attachments.serializers import AttachmentSerializer
from core.pagination import StandardPagination, ordering_from
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
    CanVerifyPayment,
    CanViewPaymentsDashboard,
    IsPaymentsApprovalAdmin,
    PAYMENTS_VERIFY,
    ReadOrCreateDeposit,
    ReadOrCreatePayment,
    granted_keys,
    has_permission_key,
)
from . import analytics, analytics_person, bank_master, hana_queries
from .models import (
    CATEGORY_CHOICES,
    BankDeposit,
    CollectionPerson,
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
)
from .serializers import (
    BankDepositCreateSerializer,
    BankDepositSerializer,
    CollectionPersonSerializer,
    PaymentReceiptCreateSerializer,
    PaymentReceiptSerializer,
    PaymentStatusHistorySerializer,
    TECHNICAL_HISTORY_ACTIONS,
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
    """Companies this user may transact in — every canonical company.

    Previously narrowed by UserPartyAssignment. That is an ORDERS concept (a
    salesperson's territory) and does not apply here: a collection agent handles
    whoever pays. Deriving company access from it also meant a user with no
    assignments silently saw a different list from one with them.

    Access to the payments module is governed by the action permissions
    (Payments_Create, Deposit_Approve, ...) — not by which parties someone sells
    to.

    Read from CATEGORY_CHOICES rather than a table: the list is OIL, BEVERAGES
    and MART, the same three the rest of the application uses, and a row per
    company existed only to carry the SAP database name that now comes from the
    environment.
    """
    return [
        {
            # A stable id per company, because the mobile client keys its
            # dropdown on one. Positional, so it survives the table's removal
            # and cannot drift the way an auto-increment could.
            'id': index,
            'company': key,
            'display_name': label,
            'is_active': True,
        }
        for index, (key, label) in enumerate(CATEGORY_CHOICES, start=1)
    ]


# ---------------------------------------------------------------------------
# Cascade: company -> parties -> open invoices
# ---------------------------------------------------------------------------

class CompanyListView(APIView):
    """Step 1. Companies (SAP categories) available to the caller."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Already plain dicts in CATEGORY_CHOICES order, so no serializer and
        # no sort: the declaration order IS the display order.
        return ok(user_companies(request.user))


def _int(value, default):
    """Query param -> int, falling back rather than 500-ing on rubbish."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dashboard_window(params):
    """(preset, date_from, date_to), or a 400 Response if the range is unusable.

    Shared by all three analytics endpoints so a custom range means the same
    thing on each — three copies of this would be three chances for the table to
    cover a different window than the chart above it.
    """
    preset = (params.get('preset') or analytics.DEFAULT_PRESET).strip().lower()
    if preset != 'custom':
        return preset, None, None

    date_from = parse_date(params.get('date_from') or '')
    date_to = parse_date(params.get('date_to') or '')
    if not date_from or not date_to:
        return fail('A custom range needs both date_from and date_to '
                    'as YYYY-MM-DD.')
    if date_from > date_to:
        # Swapped rather than rejected: the intent is unambiguous, and a date
        # picker can easily produce this order.
        date_from, date_to = date_to, date_from
    return preset, date_from, date_to


class PaymentDashboardView(APIView):
    """Every figure on the Payments Dashboard, in one response.

    One endpoint rather than one per widget, so the KPI cards and the charts are
    guaranteed to describe the same instant. Split across five requests, a
    receipt posted midway through would leave the cards disagreeing with the
    donut beside them — and a dashboard that contradicts itself gets distrusted
    for the numbers it gets right.

    Unlike the operational endpoints, this one requires an explicit grant:
    it aggregates EVERY receipt and deposit in the company, which is a wider
    view than a collector has of their own work. Hiding the menu entry is a
    usability affordance; this permission class is the boundary.
    """

    permission_classes = [IsAuthenticated, CanViewPaymentsDashboard]

    def get(self, request):
        params = request.query_params
        window = _dashboard_window(params)
        if isinstance(window, Response):
            return window
        preset, date_from, date_to = window

        return ok(analytics.dashboard(
            company=params.get('company') or '',
            preset=preset,
            date_from=date_from,
            date_to=date_to,
            search=params.get('search') or '',
            sort=params.get('sort') or 'total',
            direction=params.get('direction') or 'desc',
            page=_int(params.get('page'), 1),
            page_size=_int(params.get('page_size'), 25),
            # Same default as the standalone endpoint below. The clients read
            # page 1 from THIS payload and page 2 onward from that one, so a
            # different default here meant the table changed who it was about
            # as soon as the user paged.
            participants=(params.get('participants') or 'person').strip().lower(),
        ))


class CollectionPerformanceView(APIView):
    """Just the participants table — for paging, searching and sorting.

    The dashboard endpoint already returns page 1, so the first paint needs no
    second call. This exists so that turning a page or typing in the search box
    does not re-run the KPI and chart aggregations that have not changed.
    """

    permission_classes = [IsAuthenticated, CanViewPaymentsDashboard]

    def get(self, request):
        params = request.query_params
        window = _dashboard_window(params)
        if isinstance(window, Response):
            return window
        preset, date_from, date_to = window

        start, end = analytics.resolve_range(preset, date_from, date_to)
        return ok(analytics.collection_performance(
            (params.get('company') or '').strip().upper(), start, end,
            search=params.get('search') or '',
            sort=params.get('sort') or 'total',
            direction=params.get('direction') or 'desc',
            page=_int(params.get('page'), 1),
            page_size=_int(params.get('page_size'), 25),
            # Defaults to the collection PEOPLE — who the money came from and
            # who banked it. `user` gives per-operator activity instead, and
            # `all` the mixed list this used to return.
            participants=(params.get('participants') or 'person').strip().lower(),
        ))


class PersonAnalyticsView(APIView):
    """One participant's collection history.

    `kind` is part of the path because a CollectionPerson and a User can share
    an id and be different people — see analytics_person for why the two
    identity spaces are deliberately not merged.
    """

    permission_classes = [IsAuthenticated, CanViewPaymentsDashboard]

    def get(self, request, kind, pk):
        if kind not in ('person', 'user'):
            return fail('Unknown participant type.',
                        status=http_status.HTTP_404_NOT_FOUND)

        params = request.query_params
        window = _dashboard_window(params)
        if isinstance(window, Response):
            return window
        preset, date_from, date_to = window

        data = analytics_person.person_detail(
            kind, pk,
            company=params.get('company') or '',
            preset=preset,
            date_from=date_from,
            date_to=date_to,
        )
        if data is None:
            return fail('That person no longer exists.',
                        status=http_status.HTTP_404_NOT_FOUND)
        return ok(data)


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


class SapBranchListView(APIView):
    """SAP branches a payment may be posted to, for this company.

    Scoped to the company and to active rows — the shared /api/sap/branches/
    endpoint returns all 22 across every company and is AllowAny, so it cannot
    be used to populate a payment form.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from sap_sync.models import Branch

        company = (request.query_params.get('company') or '').strip().upper()
        if not company:
            return fail('A company is required.')
        rows = (Branch.objects
                .filter(category=company, is_active=True)
                .order_by('bpl_id')
                .values('bpl_id', 'bpl_name'))
        return ok(list(rows))


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

class CashAccountListView(APIView):
    """Cash G/L accounts a CASH payment may be received into, for a company.

    The cash counterpart of BankAccountListView: SAP is the master, the list is
    the postable children of the configured cash node, and a drawer frozen in
    SAP disappears from it on the next refresh with no OMS change.

    The client sends only the company key. The HANA schema is resolved
    server-side and never accepted from — or returned to — the caller.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()
        if not company:
            return fail('A company is required.')

        accounts, meta = bank_master.get_company_cash_accounts(
            company, force_refresh=_flag(request, 'refresh'))
        if not meta['available']:
            # Same distinction the bank list draws: "SAP says there are none"
            # is an empty list, "we could not ask" is a failure. An empty
            # dropdown would read as the former and hide a misconfiguration.
            return fail(
                'Unable to verify cash accounts because SAP is currently '
                'unavailable, or no cash account group is configured.',
                errors={'available': False})

        response = ok(accounts)
        response.data['meta'] = {'synced_at': meta['synced_at'],
                                 'stale': meta['stale'],
                                 'source': meta['source']}
        return response


def _rejection_prefetch():
    """The latest rejection per document, fetched for the WHOLE list at once.

    `get_approval` shows why a document came back, which lives in the
    append-only history. Read per row that is an N+1 — one query per receipt
    on every list render. Prefetching into `_rejections` lets the serializer
    take element 0 instead of querying, and the detail views that do not
    prefetch still work: the serializer falls back to its own query when the
    attribute is absent.
    """
    from django.db.models import Prefetch

    return Prefetch(
        'status_history',
        queryset=(PaymentStatusHistory.objects
                  .filter(action=PaymentStatusHistory.Action.REJECTED)
                  .order_by('-created_at')),
        to_attr='_rejections')


def _receipt_queryset(user):
    """Receipts visible to `user` — every configured company.

    No per-user narrowing: what someone may DO is decided by the action
    permissions, and a caller who wants only their own entries asks for them
    with `?mine=true`. Restricting reads by party assignment would hide a
    receipt from the very approver who has to decide on it.
    """
    return (
        PaymentReceipt.objects
        # `flow` and its stage are here because the serializer renders the
        # approval position on every row: without them each row cost a query
        # for the flow and another for the stage it is parked at.
        .select_related('received_from_person', 'created_by',
                        'flow', 'flow__current_stage')
        .prefetch_related('methods__denominations', 'allocations',
                          'attachments', _rejection_prefetch())
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
            ids = workflow_flow.document_ids_for_view(
                request.user, view, PaymentReceipt)
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
        # The verification queue. A plain filter on the existing list endpoint
        # rather than a dedicated /verification/ route, so it inherits the
        # pagination, company scoping and every other filter above for free —
        # and a queue that also wants `?company=` needs no second
        # implementation of it.
        #
        # NO default date window is applied here, deliberately. The plan calls
        # for the queue to open on the last 2 days, but this endpoint has never
        # defaulted its dates — `date_from`/`date_to` are opt-in — and adding a
        # server-side default would silently truncate every EXISTING caller of
        # /receipts/, which is the tracking screens. The 2-day default is a
        # property of the queue VIEW, so the client sends
        # `?verification_status=PENDING&date_from=<today-2>`; a verifier who
        # widens the range gets everything still pending, which is the correct
        # behaviour for a queue nobody may leave unworked.
        if value := request.query_params.get('verification_status'):
            qs = qs.filter(verification_status__in=[
                v.strip().upper() for v in value.split(',') if v.strip()])
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

        # EXPLICIT ORDERING, opt-in.
        #
        # The model's own default is `-payment_date, -id` — the date the
        # receipt says it happened, which a user TYPES. So an entry raised
        # today for a payment_date of last week sorts below one raised a week
        # ago, and a list nobody has filtered does not read newest-first. The
        # grouping above makes it jump further still: under "All" the rows are
        # ordered by what must be DONE about them, so they appear to move up
        # and down as their statuses change.
        #
        # `-id` is the serial the receipt was created with, so it is the one
        # ordering that always means "latest first". Requested per call rather
        # than changed in the model, because the default is what the web app
        # and the reports already read, and because the list is PAGINATED:
        # sorting on the client would reorder one page of 25 while the server
        # decided which 25 those were.
        #
        # Allow-listed via `ordering_from` — never interpolate a query param
        # into order_by().
        ordering = ordering_from(
            request, {'id', 'payment_date', 'created_at'}, '')
        if ordering:
            qs = qs.order_by(ordering)

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


#: Serializer context for any response that returns ONE document. It turns on
#: the approval ladder (`get_approval` -> `stages`), which is built per
#: document and therefore belongs on detail responses only — a list would pay
#: for it once per row to show something no row displays. Every single-document
#: response uses it, including the ones returned after approve/reject/edit, so
#: a client re-rendering from the response never loses the ladder.
DETAIL_CONTEXT = {'include_stages': True}


def _document_permissions(document, user):
    """What `user` may do with `document` — a receipt OR a deposit.

    One rule for both. The logic never depended on anything receipt-specific:
    it reads the approval chain, the creator, and the status enum that each
    model carries as `Status`. Duplicating it for deposits would have meant two
    copies of the self-approval and mid-chain-edit rules drifting apart.

    The client cannot derive these: `can_decide` depends on which rung the
    approval is parked at and whether the user is eligible for it (named
    approvers narrow a level), and self-approval is forbidden.
    """
    Status = document.__class__.Status
    flow = workflow_flow.flow_of(document)
    # A document already in SAP is finished, whatever its approval request
    # still says. Approving it again would post a second payment for the same
    # money; rejecting it would claim to undo something SAP has committed and
    # only OMS would believe. The approval row can legitimately still read
    # PENDING here — a SAP failure reopens it at the final rung, and the retry
    # that follows posts WITHOUT walking the ladder again — so `can_act` alone
    # is not enough. The document's own SAP state is the authority.
    already_in_sap = bool(
        document.sap_doc_entry
        or document.status in (Status.POSTED, Status.CANCELLED_IN_SAP))
    allowed, _reason = payment_permissions.may_act_on(user, flow, document)
    can_decide = bool(not already_in_sap and allowed)

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
    #
    # "An approver has committed" is read from the ENGINE's own position: a
    # flow still parked at sequence 1 has had no decision taken on it, and one
    # that has moved past it has. This replaces counting APPROVE rows in the
    # old engine's current round — the new engine has no round, and it does not
    # need one here: a rejection sends the document back to the creator and a
    # resubmission starts a fresh flow at sequence 1, so the position alone
    # already answers the question for the current attempt.
    stage_sequence = (getattr(flow.current_stage, 'sequence', None)
                      if flow is not None and flow.current_stage_id else None)
    approved_already = bool(stage_sequence and stage_sequence > 1)
    creator_may_edit = (
        document.created_by_id == user.id
        and (
            document.status in (Status.DRAFT,
                               Status.REJECTED,
                               Status.PENDING_ERROR)
            or (document.status == Status.PENDING_APPROVAL
                and not approved_already)
        )
    )
    # THE VERIFIER may edit while the receipt is still awaiting verification.
    # That is the point of the handover: they hold the physical money and are
    # the one person positioned to correct a mistyped amount or cheque number
    # against it. Once VERIFIED the receipt is in the approval chain and the
    # rules above take over again.
    #
    # Guarded by `hasattr` because this function is shared with BankDeposit,
    # which has no verification axis — deposits must be entirely unaffected.
    verifier_may_edit = (
        hasattr(document, 'verification_status')
        and document.verification_status == 'PENDING'
        and has_permission_key(user, PAYMENTS_VERIFY)
        # Not the creator: someone who may not verify their own receipt should
        # not gain an edit right from a permission they cannot exercise on it.
        and document.created_by_id != user.id
    )
    # A RETRY IS AN APPROVAL AT THE FINAL STAGE, but it is a different thing
    # to offer: "Approve" and "Retry SAP posting" mean different things to the
    # person holding the document, and the client must not have to work out
    # which one to show by inspecting the status itself. The backend decides,
    # here, using the same `can_decide` it already computed — so a retry can
    # never be offered to someone who may not act.
    #
    # THREE statuses mean "owed a SAP posting that has not happened", and all
    # three are retryable by the effective approver:
    #
    #   PENDING_ERROR    SAP answered "no". Nothing was committed there.
    #   SAP_UNKNOWN      SAP never answered. It may or may not hold it.
    #   POSTING_TO_SAP   with no live call — the worker died, or the call never
    #                    left. This is the one that used to have no way out at
    #                    all (RCP-OIL-20260919-000003, stuck 43 hours).
    #
    # The last two are only safe to offer because a retry is no longer blind:
    # `sap_settlement.settle` asks SAP for the document by its own reference
    # before posting, so it adopts an existing posting rather than duplicating
    # it. `can_decide` still gates all of them, so a retry can never reach
    # somebody who may not act.
    from . import sap_settlement

    owes_sap_post = (
        document.status in (Status.PENDING_ERROR, Status.SAP_UNKNOWN)
        or (document.status == Status.POSTING_TO_SAP
            and not sap_settlement.posting_is_in_flight(document))
    )
    can_retry_sap = bool(
        can_decide
        and owes_sap_post
        and not document.sap_doc_entry
        and workflow_flow.is_at_final_stage(flow))

    return {
        'can_decide': can_decide,
        # True only when the ACTION to offer is a SAP retry rather than a
        # first-time approval. `can_decide` stays true as well; this narrows it.
        'can_retry_sap': can_retry_sap,
        'can_verify': verifier_may_edit,
        'can_edit': (
            not document.sap_doc_entry
            and (can_decide or creator_may_edit or verifier_may_edit)
        ),
        # Only the creator resubmits, and only when no chain is open.
        'can_resubmit': (
            document.created_by_id == user.id
            and not document.sap_doc_entry
            and document.status in (Status.DRAFT,
                                   Status.REJECTED,
                                   Status.PENDING_ERROR)
            and not (flow is not None and flow.is_open)
        ),
    }


class PaymentReceiptDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        data = PaymentReceiptSerializer(
            receipt, context=DETAIL_CONTEXT).data
        data['permissions'] = _document_permissions(receipt, request.user)
        return ok(data)

    def patch(self, request, pk):
        """Edit a receipt's content.

        Re-checks `can_edit` here rather than trusting the client: the button is
        hidden when editing is not allowed, but a hidden button is a UI courtesy,
        not a control. This is the only thing standing between a stale app and a
        posted document being rewritten.
        """
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        permissions = _document_permissions(receipt, request.user)
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

        data = PaymentReceiptSerializer(
            receipt, context=DETAIL_CONTEXT).data
        data['permissions'] = _document_permissions(receipt, request.user)
        return ok(data, message='Receipt updated.')


class SapReceiptPdfView(APIView):
    """Stream an OMS-generated, SAP-style receipt PDF for one posted receipt.

    Built ENTIRELY from OMS data already on the receipt — no SAP call is made to
    render it. This is NOT the SAP Crystal Report file (that is a separate future
    phase). See docs/SAP_CRYSTAL_RECEIPT_INTEGRATION.md.

    Security: the client supplies only the OMS receipt id. The backend loads the
    receipt from the visible queryset, enforces per-receipt access via
    ``can_be_viewed_by``, and reads sap_doc_entry/num/trans_id from the DB — a
    client-supplied SAP DocEntry is never trusted.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        # Visibility scope first (404 for a receipt the user cannot see at all)…
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        # …then the per-receipt guard (403 for one they may not open).
        if not receipt.can_be_viewed_by(request.user):
            return fail('You do not have access to this receipt.',
                        status=http_status.HTTP_403_FORBIDDEN)

        # Posted-only: a receipt only has a SAP identity once it posted.
        if receipt.status != PaymentReceipt.Status.POSTED \
                or receipt.sap_doc_entry is None:
            return fail(
                'SAP receipt is not available because this payment has not '
                'been posted successfully.',
                status=http_status.HTTP_409_CONFLICT)

        from django.http import HttpResponse

        # ?as=png (or an image Accept header) returns a rasterised image the app
        # can show inline with <Image>; the default stays a downloadable PDF.
        # (Not `?format=` — DRF reserves that for content negotiation.)
        wants_png = (
            request.query_params.get('as', '').lower() == 'png'
            or 'image/png' in request.headers.get('Accept', '')
        )
        if wants_png:
            from .receipt_pdf import build_receipt_png
            png_bytes = build_receipt_png(receipt)
            response = HttpResponse(png_bytes, content_type='image/png')
            response['Content-Disposition'] = (
                f'inline; filename="Receipt-{receipt.receipt_no}.png"')
            response['Content-Length'] = str(len(png_bytes))
            return response

        from .receipt_pdf import build_receipt_pdf
        pdf_bytes = build_receipt_pdf(receipt)
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="Receipt-{receipt.receipt_no}.pdf"')
        response['Content-Length'] = str(len(pdf_bytes))
        return response


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


class PaymentReceiptVerifyView(APIView):
    """Verify a receipt (the handover check) and send it into approval.

    Three independent checks, and all three are required:

      * `CanVerifyPayment` — the capability, from the permission registry.
      * The receipt is still PENDING and not already in SAP — record state,
        re-read under a row lock in the service.
      * The verifier is not the creator — separation of duties.

    The object is fetched through `_receipt_queryset`, the SAME queryset every
    other receipt endpoint uses, so verification cannot become a way to reach
    a document the rest of the module would not show. Holding Payments_Verify
    grants no extra visibility.
    """

    permission_classes = [IsAuthenticated, CanVerifyPayment]

    def post(self, request, pk):
        # 404 before the service runs, so an unknown id never reaches the lock.
        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        remarks = (request.data.get('verification_remarks')
                   or request.data.get('remarks') or '').strip()
        try:
            services.verify_receipt(
                receipt.pk, request.user,
                remarks=remarks, ctx=request_context(request))
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))

        receipt.refresh_from_db()
        data = PaymentReceiptSerializer(
            receipt, context=DETAIL_CONTEXT).data
        data['permissions'] = _document_permissions(receipt, request.user)
        return ok(data, message='Payment verified and submitted for approval.')


class PaymentReceiptHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from django.contrib.contenttypes.models import ContentType

        receipt = get_object_or_404(_receipt_queryset(request.user), pk=pk)
        rows = (
            PaymentStatusHistory.objects
            .filter(
                content_type=ContentType.objects.get_for_model(PaymentReceipt),
                object_id=receipt.pk,
            )
            # Bare STATUS_CHANGED rows are excluded: they record that a status
            # moved without saying who or why, which reads as noise between the
            # events that do carry meaning. Nothing is deleted — every row is
            # still stored and still visible in the Django admin, which is the
            # forensic view. `?full=true` returns them for support work.
            .exclude(
                **({} if _flag(request, 'full')
                   else {'action__in': list(TECHNICAL_HISTORY_ACTIONS)})
            )
            # Oldest first: a timeline is read top-down, and the default model
            # ordering is newest-first for the admin's benefit.
            .order_by('created_at', 'id')
        )
        return ok(PaymentStatusHistorySerializer(rows, many=True).data)


class BankDepositHistoryView(APIView):
    """The deposit's business timeline — the same shape as a receipt's.

    Deposits went without this while receipts had it, so a deposit's progress
    screen could only ever show the remark stored on the document itself: the
    creator's. Every later remark — an approver's reason, a SAP outcome — was
    written to PaymentStatusHistory and then never read back by any client.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from django.contrib.contenttypes.models import ContentType

        deposit = get_object_or_404(_deposit_queryset(request.user), pk=pk)
        rows = (
            PaymentStatusHistory.objects
            .filter(
                content_type=ContentType.objects.get_for_model(BankDeposit),
                object_id=deposit.pk,
            )
            # Same exclusion as the receipt timeline: bare STATUS_CHANGED rows
            # say a status moved without saying who or why. `?full=true`
            # returns them for support work.
            .exclude(
                **({} if _flag(request, 'full')
                   else {'action__in': list(TECHNICAL_HISTORY_ACTIONS)})
            )
            .order_by('created_at', 'id')
        )
        return ok(PaymentStatusHistorySerializer(rows, many=True).data)


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

def _deposit_queryset(user):
    """Deposits visible to `user` — see _receipt_queryset."""
    return (
        BankDeposit.objects
        .select_related('deposited_by', 'created_by',
                        'flow', 'flow__current_stage')
        .prefetch_related(
            # The line serializer reads each receipt's methods and collector,
            # so both are prefetched: without them a deposit with N receipts
            # costs a query per receipt for the tenders and another for the
            # collector — the difference between 15 queries and 6 on a single
            # deposit, and it grows with the number of receipts banked.
            'lines__receipt__methods',
            'lines__receipt__received_from_person',
            'attachments',
            _rejection_prefetch(),
        )
    )


class BankDepositListCreateView(APIView):
    # Read for any authenticated user; creating requires Deposit_Create.
    permission_classes = [IsAuthenticated, ReadOrCreateDeposit]

    def get(self, request):
        qs = _deposit_queryset(request.user)

        # Approver-relative views — the same contract the receipts list honours
        # (PaymentReceiptListCreateView). Without this the parameter was
        # accepted and silently IGNORED, so an approver who picked "Pending" on
        # Deposit Tracking got every deposit back, POSTED and PENDING_ERROR
        # included: the filter looked applied and was not.
        #
        # `status` alone cannot express these: whether a PENDING_APPROVAL
        # deposit is waiting on THIS user depends on which rung it stopped at,
        # which lives on the approval request rather than the deposit.
        if view := (request.query_params.get('approval_view') or '').strip():
            ids = workflow_flow.document_ids_for_view(
                request.user, view, BankDeposit)
            if ids is not None:
                qs = qs.filter(id__in=ids)

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

        # "All" orders by what the user must DO about each entry rather than by
        # date — attention first, finished work last. Mirrors the receipts list
        # so both tracking screens read the same way.
        if _flag(request, 'group_by_status'):
            qs = qs.annotate(
                _rank=Case(
                    When(status=BankDeposit.Status.PENDING_APPROVAL, then=0),
                    When(status__in=[BankDeposit.Status.APPROVED,
                                     BankDeposit.Status.POSTING_TO_SAP],
                         then=1),
                    When(status__in=[BankDeposit.Status.REJECTED,
                                     BankDeposit.Status.PENDING_ERROR,
                                     BankDeposit.Status.SAP_UNKNOWN,
                                     # Posted then reversed in SAP — it needs a
                                     # decision, so it ranks with the problems
                                     # rather than falling to the default.
                                     BankDeposit.Status.CANCELLED_IN_SAP],
                         then=2),
                    When(status=BankDeposit.Status.POSTED, then=3),
                    default=4,
                    output_field=IntegerField(),
                )
            ).order_by('_rank', '-deposit_date', '-id')

        # EXPLICIT ORDERING, opt-in.
        #
        # The model's own default is `-deposit_date, -id` — the date the
        # deposit says it happened, which a user TYPES. So an entry raised
        # today for a deposit_date of last week sorts below one raised a week
        # ago, and a list nobody has filtered does not read newest-first. The
        # grouping above makes it jump further still: under "All" the rows are
        # ordered by what must be DONE about them, so they appear to move up
        # and down as their statuses change.
        #
        # `-id` is the serial the deposit was created with, so it is the one
        # ordering that always means "latest first". Requested per call rather
        # than changed in the model, because the default is what the web app
        # and the reports already read, and because the list is PAGINATED:
        # sorting on the client would reorder one page of 25 while the server
        # decided which 25 those were.
        #
        # Allow-listed via `ordering_from` — never interpolate a query param
        # into order_by().
        ordering = ordering_from(
            request, {'id', 'deposit_date', 'created_at'}, '')
        if ordering:
            qs = qs.order_by(ordering)

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
        data = BankDepositSerializer(
            deposit, context=DETAIL_CONTEXT).data
        # Same shape a receipt returns, from the same helper — so the app can
        # gate the approve/reject bar identically for both documents.
        data['permissions'] = _document_permissions(deposit, request.user)
        return ok(data)

    def patch(self, request, pk):
        """Edit a deposit's content — the mirror of PaymentReceiptDetailView.

        Exists so a deposit SAP refused can be corrected and sent round again
        rather than cancelled and retyped. `can_edit` is re-checked here, not
        trusted from the client: hiding the button is a courtesy, and this is
        the only thing standing between a stale app and a posted document being
        rewritten.
        """
        deposit = get_object_or_404(_deposit_queryset(request.user), pk=pk)
        permissions = _document_permissions(deposit, request.user)
        if not permissions['can_edit']:
            reason = (
                f'{deposit.deposit_no} is already posted to SAP and cannot be '
                f'changed.'
                if deposit.sap_doc_entry else
                f'{deposit.deposit_no} can no longer be edited — it has been '
                f'approved or is not yours to change.'
            )
            return fail(reason, status=http_status.HTTP_403_FORBIDDEN)

        serializer = BankDepositCreateSerializer(
            deposit, data=request.data, partial=True,
            context={'request': request})
        if not serializer.is_valid():
            return fail('Could not update the deposit.',
                        errors=serializer.errors)
        try:
            deposit = serializer.save()
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages))

        data = BankDepositSerializer(
            deposit, context=DETAIL_CONTEXT).data
        data['permissions'] = _document_permissions(deposit, request.user)
        return ok(data, message='Deposit updated.')


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

        # When EDITING a deposit, its own receipts must still be offered —
        # they are banked, but banked HERE. Without this the edit form loads
        # with an empty picker and the selection it was prefilled with cannot
        # be seen, changed, or totalled.
        editing_id = _int(request.query_params.get('deposit'), 0)

        unbanked = Q(deposit_lines__isnull=True)
        if editing_id:
            unbanked |= Q(deposit_lines__deposit_id=editing_id)

        qs = (
            _receipt_queryset(request.user)
            # "Not yet banked" is the absence of a BankDepositLine, which is the
            # only link between a receipt and a deposit and carries the unique
            # constraint that stops a receipt being banked twice.
            .filter(Q(company=company,
                      status=PaymentReceipt.Status.POSTED) & unbanked)
            # Physical tenders only — CASH and CHEQUE (see
            # PaymentMethodEntry.DEPOSITABLE_METHODS, the single definition
            # this and services.validate_deposit both read).
            #
            # Two clauses, not one. The filter alone matches a MIXED
            # cash+UPI receipt because it HAS a depositable line, and
            # validate_deposit would then reject it at submit. The exclude is
            # the NOT-EXISTS half: drop any receipt carrying even one
            # non-depositable line.
            #
            # The subquery is deliberate. `.exclude(~Q(methods__method__in=...))`
            # reads equivalently but is NOT — across a multi-valued join it
            # evaluates per row, so a mixed receipt survives on its cash row.
            # Verified against live data, where that form wrongly returned two
            # mixed receipts.
            .filter(methods__method__in=PaymentMethodEntry.DEPOSITABLE_METHODS)
            .exclude(id__in=PaymentMethodEntry.objects
                     .exclude(method__in=PaymentMethodEntry.DEPOSITABLE_METHODS)
                     .values('receipt_id'))
            # A receipt with several tender lines matches the join once each.
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

        # Recorded on the document's timeline, not just in the attachment
        # table. An upload arrives through its own endpoint and never touches
        # the document's `update()`, so nothing used to write a history row and
        # the Edit History card could not show it.
        #
        # But only a LATER upload is an edit. The client attaches the cheque
        # image moments after POSTing the receipt — that is part of raising it,
        # not a change to it — so logging that as UPDATED put an "edited"
        # entry on every receipt the instant it was created, before anyone had
        # touched it. The Edit History card is meant to answer "what changed
        # after this was raised", and a creation-time attachment is not that.
        #
        # The test is the document's own lifecycle, not a time window: while it
        # is still DRAFT nobody downstream has seen it, so there is nothing to
        # have changed FROM. Once it has been verified or sent for approval,
        # every further attachment genuinely alters what an approver is signing
        # off and is logged as UPDATED with a one-field diff.
        from .services import log_status
        is_initial = document.status == self.model.Status.DRAFT
        try:
            rows = list(attachment_services.for_document(document))
            after = sorted(a.get_attachment_type_display() for a in rows)
            # Excluded BY ID, not by type name: two cheque images are two
            # separate files, and filtering on the label would show the first
            # one disappearing when the second was added.
            before = sorted(a.get_attachment_type_display()
                            for a in rows if a.pk != attachment.pk)
            log_status(
                document,
                from_status=document.status, to_status=document.status,
                user=request.user,
                # CREATED, not UPDATED, for the first attachment. The Edit
                # History card selects rows by `action === 'UPDATED'`, so this
                # is what actually keeps a creation-time file off it — a null
                # diff alone would still render as an edit with no detail. The
                # row is still written, so the file remains visible on the main
                # timeline where it belongs.
                action=(PaymentStatusHistory.Action.CREATED if is_initial
                        else PaymentStatusHistory.Action.UPDATED),
                # No diff on the initial attachment: `change_data` is what the
                # card renders as "old -> new", and there is no meaningful
                # "old" for a file that arrived with the document.
                change_data=(
                    None if is_initial
                    else {'attachments': {'old': before, 'new': after}}
                ),
                reason=f'Attached {attachment.get_attachment_type_display()}.',
            )
        except Exception:                                   # noqa: BLE001
            # The file is already stored and the row already written — a
            # failure to log must not turn a successful upload into an error.
            logger.exception('Could not log attachment upload for %s %s',
                             self.model.__name__, pk)

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

    permission_classes = [IsAuthenticated, IsPaymentsApprovalAdmin]
    serializer_class = CollectionPersonSerializer

    def get_queryset(self):
        qs = CollectionPerson.objects.all()
        if company := (self.request.query_params.get('company') or '').strip().upper():
            # Company-specific rows PLUS the all-companies ones, matching what
            # the picker feed returns.
            qs = qs.filter(Q(company=company) | Q(company=''))
        return qs.order_by('company', 'name')


class CollectionPersonAdminDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, IsPaymentsApprovalAdmin]
    serializer_class = CollectionPersonSerializer
    queryset = CollectionPerson.objects.all()



def request_context(request):
    """IP and user agent for the audit trail, from the request alone."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return {
        'ip': (forwarded.split(',')[0].strip()
               or request.META.get('REMOTE_ADDR')),
        'user_agent': request.META.get('HTTP_USER_AGENT', '')[:400],
    }


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


# ---------------------------------------------------------------------------
# Approval — payments' own, over the Workflow Engine
# ---------------------------------------------------------------------------
#
# These replace the old generic `/api/approvals/requests/<id>/act/`. The engine
# owns configuration and selection only; deciding a payment is payments' job,
# so the endpoint lives here beside the document it decides.


class _DecisionView(APIView):
    """Shared body for approve / reject / cancel on either document type.

    Authorisation is deliberately two-part and the two failures are told apart:
    403 when this user may not act, 409 when the document is not in a state to
    be acted on. A client that cannot distinguish them shows "permission
    denied" to someone whose only problem is that a colleague got there first.
    """

    permission_classes = [IsAuthenticated]
    model = None

    def _load(self, request, pk):
        document = get_object_or_404(self.model, pk=pk)
        flow = workflow_flow.flow_of(document)
        allowed, reason = payment_permissions.may_act_on(
            request.user, flow, document)
        if not allowed:
            return document, flow, fail(reason,
                                        status=http_status.HTTP_403_FORBIDDEN)
        return document, flow, None


class _ApproveView(_DecisionView):
    def post(self, request, pk):
        document, flow, refusal = self._load(request, pk)
        if refusal is not None:
            return refusal
        try:
            workflow_flow.approve(flow, user=request.user,
                                  remarks=(request.data.get('remarks') or ''))
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages),
                        status=http_status.HTTP_409_CONFLICT)

        # THE SAP POST, HERE, EXPLICITLY — not on `transaction.on_commit`.
        #
        # `approve()` has returned and its transaction has committed, so the
        # intent (POSTING_TO_SAP) is durable. This call settles it. It is
        # deliberately OUTSIDE the try above: a SAP refusal is not an approval
        # failure, it is an outcome, and it comes back on the document for the
        # approver to read and retry.
        #
        # Never raises for a SAP-side problem — `post_document` records the
        # failure on the document and returns it. Only an unreachable HANA
        # during the interrupted-post VERIFICATION can raise, and that must not
        # lose the approval either: the intent stays committed and the retry
        # stays available.
        fresh = document.__class__.objects.get(pk=pk)
        try:
            workflow_flow.settle_after_approval(fresh, user=request.user)
        except Exception:                                        # noqa: BLE001
            logger.exception('SAP settlement failed for %s %s',
                             document.__class__.__name__, pk)

        return ok(self.serializer(document.__class__.objects.get(pk=pk)).data,
                  message='Approved.')


class _RejectView(_DecisionView):
    def post(self, request, pk):
        document, flow, refusal = self._load(request, pk)
        if refusal is not None:
            return refusal
        remarks = (request.data.get('remarks') or '').strip()
        if not remarks:
            # A validation problem, not a state conflict: the client can fix it
            # by asking for a reason.
            return fail('A reason is required when rejecting.')
        try:
            workflow_flow.reject(flow, user=request.user, remarks=remarks)
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages),
                        status=http_status.HTTP_409_CONFLICT)
        return ok(self.serializer(document.__class__.objects.get(pk=pk)).data,
                  message='Rejected.')


class _CancelView(APIView):
    """Withdraw a document from approval. The SUBMITTER's escape hatch.

    Not `may_act_on`: cancelling is the creator's right over their own
    document, not an approver's decision.
    """

    permission_classes = [IsAuthenticated]
    model = None

    def post(self, request, pk):
        document = get_object_or_404(self.model, pk=pk)
        if document.created_by_id != request.user.id \
                and not request.user.is_staff:
            return fail('Only the person who raised this document may cancel '
                        'it.', status=http_status.HTTP_403_FORBIDDEN)
        flow = workflow_flow.flow_of(document)
        try:
            workflow_flow.cancel(flow, user=request.user,
                                 remarks=(request.data.get('remarks') or ''))
        except DjangoValidationError as exc:
            return fail('; '.join(exc.messages),
                        status=http_status.HTTP_409_CONFLICT)
        return ok(self.serializer(document.__class__.objects.get(pk=pk)).data,
                  message='Cancelled.')


class PaymentReceiptApproveView(_ApproveView):
    model = PaymentReceipt
    serializer = staticmethod(PaymentReceiptSerializer)


class PaymentReceiptRejectView(_RejectView):
    model = PaymentReceipt
    serializer = staticmethod(PaymentReceiptSerializer)


class PaymentReceiptCancelView(_CancelView):
    model = PaymentReceipt
    serializer = staticmethod(PaymentReceiptSerializer)


class BankDepositApproveView(_ApproveView):
    model = BankDeposit
    serializer = staticmethod(BankDepositSerializer)


class BankDepositRejectView(_RejectView):
    model = BankDeposit
    serializer = staticmethod(BankDepositSerializer)


class BankDepositCancelView(_CancelView):
    model = BankDeposit
    serializer = staticmethod(BankDepositSerializer)


class PaymentApprovalQueueView(APIView):
    """Everything waiting for THIS user to decide, both document types.

    Replaces the old generic inbox. Resolved from the workflow stages the user
    may act at today — including stages they cover as a stand-in — never from a
    user copied onto a row when it was created.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = (request.query_params.get('company') or '').strip().upper()

        receipts = PaymentReceipt.objects.filter(
            id__in=workflow_flow.pending_receipt_ids(request.user))
        deposits = BankDeposit.objects.filter(
            id__in=workflow_flow.pending_deposit_ids(request.user))
        if company:
            receipts = receipts.filter(company=company)
            deposits = deposits.filter(company=company)

        return ok({
            'receipts': PaymentReceiptSerializer(
                receipts.order_by('-payment_date', '-id'), many=True).data,
            'deposits': BankDepositSerializer(
                deposits.order_by('-deposit_date', '-id'), many=True).data,
        })
