"""Serializers with REAL nested writes.

No nested-write serializer exists anywhere in this project — order+items uses
`ListField(DictField())` (orders/serializers.py:99), which applies zero
validation to any child field, and the rows are written by a manual view helper
with no transaction. Here the children are typed serializers and `create()` is
atomic.
"""
import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from attachments.serializers import AttachmentSerializer
from core.models import next_document_number

from django.core.exceptions import ValidationError as DjangoValidationError

import logging

from . import bank_master, hana_queries
from . import services as payment_services
from .models import (
    BankDeposit,
    BankDepositLine,
    CashDenomination,
    CollectionPerson,
    PaymentAllocation,
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
)


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


class CollectionPersonSerializer(serializers.ModelSerializer):
    # `code` is globally unique but is an internal identifier the admin has no
    # reason to invent — generated from the name when omitted.
    code = serializers.CharField(required=False, allow_blank=True, max_length=30)

    class Meta:
        model = CollectionPerson
        fields = ['id', 'name', 'code', 'company', 'phone', 'is_active']

    def validate_code(self, value):
        value = (value or '').strip().upper()
        if not value:
            return value
        qs = CollectionPerson.objects.filter(code=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                f'The code "{value}" is already used by another person.')
        return value

    def create(self, validated_data):
        if not validated_data.get('code'):
            validated_data['code'] = self._generate_code(validated_data['name'])
        return super().create(validated_data)

    @staticmethod
    def _generate_code(name):
        """A stable, readable code derived from the name, e.g. 'NAVNEET-3'.

        The numeric suffix rises until it is free, so two people with the same
        name never collide on the unique constraint.
        """
        base = ''.join(ch for ch in (name or '').upper() if ch.isalnum())[:20] or 'PERSON'
        if not CollectionPerson.objects.filter(code=base).exists():
            return base
        suffix = 2
        while CollectionPerson.objects.filter(code=f'{base}-{suffix}').exists():
            suffix += 1
        return f'{base}-{suffix}'


class CashDenominationSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True)

    class Meta:
        model = CashDenomination
        fields = ['id', 'denomination', 'quantity', 'line_total']


# A UTR is bank-issued: alphanumeric, sometimes with a separator. Anything
# else is a typo or a pasted label, and both break reconciliation.
UPI_REFERENCE_MAX = 50
UPI_REFERENCE_RE = re.compile(r'[A-Za-z0-9/-]+')


class PaymentMethodEntrySerializer(serializers.ModelSerializer):
    denominations = CashDenominationSerializer(many=True, required=False)
    # Where OMS banks this line, resolved live from the admin mapping. Read
    # only, and kept separate from `bank_name` (the CUSTOMER's bank on a
    # cheque) so the detail screen can show the two without conflating them.
    deposit_account = serializers.SerializerMethodField()

    class Meta:
        model = PaymentMethodEntry
        fields = ['id', 'method', 'amount', 'upi_reference', 'cheque_number',
                  'bank_name', 'cheque_date', 'sap_check_key', 'denominations',
                  'deposit_account',
                  # The account the user chose. `account_key` is the only one a
                  # client may send; the rest are the server's snapshot of what
                  # that key resolved to, so a caller can never name the G/L
                  # its money posts to.
                  'account_key', 'gl_account', 'bank_code',
                  'receiving_bank_name', 'account_number', 'branch']
        read_only_fields = ['sap_check_key', 'gl_account', 'bank_code',
                            'receiving_bank_name', 'account_number', 'branch']
        extra_kwargs = {
            # Optional during the transition: app builds released before the
            # account picker send no key, and must keep working. Those receipts
            # carry no snapshot and still resolve through the method mapping at
            # posting time, exactly as they do today.
            'account_key': {'required': False, 'allow_blank': True},
        }

    def get_deposit_account(self, obj):
        """Our account for this tender, or None when not configured.

        SKIPPED FOR LISTS, like `sap_branch` above. Resolving it reads the
        admin's method->account mapping (and, for a non-cash tender, SAP's own
        house-bank master) once PER TENDER LINE — 37 mapping queries on a
        49-row list, for a field only the detail screen renders.

        Detected by walking UP to the outermost serializer and asking whether
        THAT one is a list. The nesting is always
        `methods ListSerializer -> receipt serializer [-> receipt
        ListSerializer]`, so a nested list alone proves nothing: `methods` is
        a list on a detail page too. Only the root tells them apart.
        """
        root = self
        while root.parent is not None:
            root = root.parent
        if isinstance(root, serializers.ListSerializer):
            return None

        # READ FROM THE LINE, never resolved again.
        #
        # This used to ask `PaymentMethodMapping` what account the method maps
        # to TODAY, which meant an old receipt displayed wherever the current
        # configuration points — not where its money actually went. The line
        # now carries the account it was received into, so the answer is the
        # historical one.
        #
        # Blank for a line raised before the picker and already settled: those
        # were deliberately not back-filled, because the mapping had been
        # edited since they posted and stamping today's value would have
        # recorded an account SAP may never have used. Saying nothing is
        # honest; guessing is not.
        if not (obj.gl_account or '').strip():
            return None
        return {'bank_name': obj.receiving_bank_name or (
                    'Cash' if obj.method == PaymentMethodEntry.Method.CASH
                    else ''),
                'gl_account': obj.gl_account,
                'account_number': obj.account_number,
                'branch': obj.branch}

    def validate(self, attrs):
        method = attrs.get('method')

        if method == PaymentMethodEntry.Method.UPI:
            # OPTIONAL. A UTR is not always to hand when the receipt is raised,
            # so it is not demanded — but when one IS given it is cleaned and
            # checked, because a malformed reference is worse than none: it
            # looks reconcilable and is not.
            reference = (attrs.get('upi_reference') or '').strip()
            if reference:
                if len(reference) > UPI_REFERENCE_MAX:
                    raise serializers.ValidationError({
                        'upi_reference':
                            f'Must be {UPI_REFERENCE_MAX} characters or fewer.'})
                if not UPI_REFERENCE_RE.fullmatch(reference):
                    raise serializers.ValidationError({
                        'upi_reference':
                            'Use only letters, numbers, hyphens and slashes.'})
            # Store trimmed either way, so a stray space can never make two
            # records of the same UTR look different.
            attrs['upi_reference'] = reference

        if method == PaymentMethodEntry.Method.CHEQUE:
            # The CUSTOMER's bank, as printed on the cheque — not one of ours,
            # so it is deliberately NOT validated against our accounts. It is
            # sent to SAP verbatim as BankCode. Uppercased here as well as in
            # the UI so the stored value is consistent whatever the client.
            if attrs.get('bank_name'):
                attrs['bank_name'] = attrs['bank_name'].strip().upper()
            if not attrs.get('cheque_number'):
                raise serializers.ValidationError(
                    {'cheque_number': 'Required for a cheque payment.'})
            if not attrs.get('cheque_date'):
                raise serializers.ValidationError(
                    {'cheque_date': 'Required for a cheque payment.'})

        # The cash breakdown must be present AND equal the cash amount. This
        # mirrors the rule enforced in the mobile UI.
        #
        # It is REQUIRED, not optional: the breakdown is the count of the notes
        # physically handed over, and a cash receipt without one cannot be
        # reconciled against what the collector is carrying. Previously the
        # whole block was skipped when the list was empty, so a cash receipt
        # could be created for money nobody had counted.
        rows = attrs.get('denominations') or []
        amount = attrs.get('amount') or Decimal('0')
        if rows:
            if method != PaymentMethodEntry.Method.CASH:
                raise serializers.ValidationError(
                    {'denominations': 'Only a cash entry may carry denominations.'})
            counted = sum(
                (Decimal(r['denomination']) * r['quantity'] for r in rows),
                Decimal('0'))
            if counted != amount:
                raise serializers.ValidationError({
                    'denominations':
                        f'Denominations total {counted} but the cash amount is '
                        f'{amount}.'})
        elif method == PaymentMethodEntry.Method.CASH and amount > 0:
            raise serializers.ValidationError({
                'denominations':
                    'The cash note breakdown is required for a cash payment.'})
        return attrs


class PaymentAllocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentAllocation
        fields = ['id', 'sap_doc_entry', 'sap_doc_num', 'invoice_type',
                  'invoice_date', 'invoice_due_date', 'invoice_total',
                  'balance_at_selection', 'amount_applied']

    def validate_amount_applied(self, value):
        if value <= 0:
            raise serializers.ValidationError('Must be greater than zero.')
        return value


# Which business stage an action belongs to. Derived from the ACTION rather
# than stored on the row: the stage is a property of the event ("verifying" is
# what a VERIFIED row means), so storing it would be a second, forgeable copy
# of a fact the action already carries — and every historical row would need
# backfilling with a value nobody recorded at the time.
#
# The keys are the permission keys the stages correspond to, so the label a
# client shows matches the permission an admin grants.
_STAGE_BY_ACTION = {
    PaymentStatusHistory.Action.CREATED: ('Payments_Create', 'Payments — Create'),
    PaymentStatusHistory.Action.UPDATED: ('Payments_Create', 'Payments — Create'),
    PaymentStatusHistory.Action.VERIFIED: ('Payments_Verify', 'Payments — Verify'),
    PaymentStatusHistory.Action.SUBMITTED: ('Payments_Create', 'Payments — Create'),
    PaymentStatusHistory.Action.RESUBMITTED: ('Payments_Create', 'Payments — Create'),
    PaymentStatusHistory.Action.APPROVED: ('Payments_Approve', 'Payments — Approve'),
    PaymentStatusHistory.Action.REJECTED: ('Payments_Approve', 'Payments — Approve'),
    PaymentStatusHistory.Action.RETURNED: ('Payments_Approve', 'Payments — Approve'),
    PaymentStatusHistory.Action.SAP_POST_STARTED: ('SAP', 'SAP Posting'),
    PaymentStatusHistory.Action.SAP_POSTED: ('SAP', 'SAP Posting'),
    PaymentStatusHistory.Action.SAP_FAILED: ('SAP', 'SAP Posting'),
    PaymentStatusHistory.Action.SAP_UNKNOWN: ('SAP', 'SAP Posting'),
    PaymentStatusHistory.Action.SAP_CANCELLED: ('SAP', 'SAP Posting'),
}

#: Rows that carry no business meaning on their own. A bare STATUS_CHANGED is
#: the fallback written when a caller records no action — it says a status
#: moved without saying who or why, which is noise in a business timeline and
#: is exactly what the technical view (Django admin) is for. The rows are NOT
#: deleted; they are simply not part of this representation.
TECHNICAL_HISTORY_ACTIONS = frozenset({
    PaymentStatusHistory.Action.STATUS_CHANGED,
})


class PaymentStatusHistorySerializer(serializers.ModelSerializer):
    """The BUSINESS timeline: who did what, at which stage, and why.

    Deliberately excludes the technical columns — `from_status`, `to_status`,
    and `level_label`. Those are still written and still stored; they are
    forensic detail, readable through the Django admin. Showing an internal
    state transition to a collector answers a question they never asked.

    `actor_kind`, `ip_address` and the SAP document columns were dropped from
    the table entirely in migration 0031 — this serializer never exposed them,
    so its output is unchanged.

    SAP DocEntry is likewise omitted: it lives on the receipt, which is its
    authoritative home, and repeating it per row invites the two disagreeing.

    `change_data` IS included, and is the exception that proves the rule: it is
    business detail, not forensic — "the amount went from 1,000 to 1,200" is
    exactly what a reader wants from an edit. It is null on every row but an
    edit, so a client must handle null rather than assume the shape.
    """

    action_display = serializers.CharField(
        source='get_action_display', read_only=True)
    stage = serializers.SerializerMethodField()
    stage_display = serializers.SerializerMethodField()
    performed_by = serializers.SerializerMethodField()
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PaymentStatusHistory
        fields = ['id', 'action', 'action_display',
                  'stage', 'stage_display',
                  'level', 'performed_by', 'performed_by_name',
                  'changed_by_username',
                  'reason', 'change_data', 'created_at']

    @extend_schema_field(serializers.CharField())
    def get_performed_by_name(self, obj):
        """The actor's FULL name, for a reader who does not know usernames.

        The history stores only the username — deliberately, since text
        outlives a deleted account and an audit row must not blank out when
        someone leaves. So the name is resolved here, against the live user
        table, and falls back to the username when the account is gone. That
        fallback is the point: the row still names who acted.

        Resolved ONCE for the whole list and cached on the serializer context,
        not per row. A timeline of 20 events would otherwise issue 20 queries
        to put a name on each one.
        """
        username = obj.changed_by_username
        if not username:
            return 'System'

        names = self.context.get('_history_names')
        if names is None:
            from django.contrib.auth import get_user_model
            root = self
            while root.parent is not None:
                root = root.parent
            rows = getattr(root, 'instance', None) or []
            usernames = {
                r.changed_by_username for r in rows
                if getattr(r, 'changed_by_username', '')
            } or {username}
            names = dict(
                get_user_model().objects
                .filter(username__in=usernames)
                .values_list('username', 'name')
            )
            self.context['_history_names'] = names

        return names.get(username) or username

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_stage(self, obj):
        """The permission key this event belongs to, or None."""
        mapped = _STAGE_BY_ACTION.get(obj.action)
        return mapped[0] if mapped else None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_stage_display(self, obj):
        mapped = _STAGE_BY_ACTION.get(obj.action)
        return mapped[1] if mapped else None

    @extend_schema_field(serializers.CharField())
    def get_performed_by(self, obj):
        """Who did it, ready to display.

        Replaces `actor_kind` as a separate technical column: an event with no
        user is the system acting, so it says "System" rather than leaking
        'SAP' / 'APPROVAL_ENGINE' as a value the reader has to decode.
        """
        return obj.changed_by_username or 'System'


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

class PaymentReceiptSerializer(serializers.ModelSerializer):
    """Read shape for lists and detail."""

    methods = PaymentMethodEntrySerializer(many=True, read_only=True)
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    unallocated_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True)
    approval = serializers.SerializerMethodField()
    # The branch this receipt WILL post to, and where that came from, so the
    # UI can show it before posting and label it correctly: an invoice payment
    # inherits it and must not be editable, an advance is the user's choice.
    sap_branch = serializers.SerializerMethodField()
    # Who raised this entry. Stored since day one but never exposed, so no
    # client could show it — the list and detail screens both need it.
    created_by_name = serializers.CharField(
        source='created_by.name', read_only=True, default='')
    created_by_username = serializers.CharField(
        source='created_by.username', read_only=True, default='')
    # Who performed the handover check, in the same representation style as
    # created_by above: name and username only, no other account detail.
    verification_status_display = serializers.CharField(
        source='get_verification_status_display', read_only=True, default='')
    verified_by_name = serializers.CharField(
        source='verified_by.name', read_only=True, default='')
    verified_by_username = serializers.CharField(
        source='verified_by.username', read_only=True, default='')
    # Who CAN verify this receipt, so a creator whose payment is sitting
    # unverified knows exactly whom to chase. Resolved live from the permission
    # grants on every read, so adding or removing a verifier takes effect
    # immediately — there is no stored list to go stale.
    eligible_verifiers = serializers.SerializerMethodField()

    class Meta:
        model = PaymentReceipt
        fields = ['id', 'receipt_no', 'company', 'card_code', 'card_name',
                  'received_from_type', 'received_from_person', 'received_from_name',
                  'payment_date', 'is_advance', 'total_amount', 'allocated_amount',
                  'unallocated_amount', 'currency', 'remarks',
                  'status', 'status_display',
                  # sap_trans_id is the journal-entry key: DocEntry finds the
                  # payment, TransId finds its accounting in JDT1. Additive —
                  # null on every document posted before it was captured.
                  'sap_doc_entry', 'sap_doc_num', 'sap_trans_id',
                  'sap_posted_at',
                  'sap_branch_id', 'sap_branch_name', 'sap_branch',
                  'sap_response', 'sap_raw_error', 'sap_raw_error_code',
                  # SAP-side cancellation, kept apart from sap_response so the
                  # original posting confirmation stays readable.
                  'sap_cancelled_at', 'sap_cancellation_response',
                  'sap_reconciled_at',
                  'methods', 'allocations', 'attachments', 'approval',
                  'created_by', 'created_by_name', 'created_by_username',
                  # The handover gate. Orthogonal to `status` above — a receipt
                  # carries both, and the verification axis never rewinds.
                  'verification_status', 'verification_status_display',
                  'verified_by', 'verified_by_name', 'verified_by_username',
                  'verified_at', 'verification_remarks', 'eligible_verifiers',
                  'created_at', 'updated_at']
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_eligible_verifiers(self, obj):
        """Who could verify this receipt right now.

        Only while it is actually waiting: once verified the answer is
        `verified_by`, and listing candidates then would invite someone to
        chase a step already done.

        Two exclusions, both so the list names people worth chasing:

        * The CREATOR — the server refuses their own verification, so naming
          them would send the creator to themselves.
        * ADMINISTRATORS — an admin holds every key implicitly (see
          `granted_keys`), so including them listed most of the office and
          buried the two or three people actually assigned to this job. An
          admin can still verify; they are simply not who you go and ask.

        What remains is the explicit grant: users an administrator ticked
        `Payments_Verify` for. Resolved per read, so a verifier added or
        removed on the permissions page takes effect immediately.
        """
        if obj.verification_status != PaymentReceipt.VerificationStatus.PENDING:
            return []

        # Resolving this needs a scan of the user table (`extra_pages` is a
        # JSON list, so the key test cannot be a column filter). That is fine
        # once for a detail page and wasteful once PER ROW of a list, so the
        # holder set is resolved ONCE and cached on the serializer context —
        # which lives for exactly one request.
        holders = self.context.get('_verifier_cache')
        if holders is None:
            from django.contrib.auth import get_user_model
            from django.db.models import Q

            from core.permissions import ADMIN_ROLE

            from .permissions import PAYMENTS_VERIFY

            User = get_user_model()
            # Administrators are identified in SQL and then EXCLUDED.
            #
            # Resolved as one query rather than by calling
            # `core.permissions.is_admin` per user: that helper goes through
            # `User.all_role_names()`, which issues its own `values_list` on
            # the extra_roles M2M and so defeats any prefetch — it cost 133
            # queries for seven candidates against the live data. The condition
            # below is the same rule (admin role held as primary OR extra, or
            # is_staff, or is_superuser).
            admin_ids = set(
                User.objects.filter(is_active=True)
                .filter(
                    Q(role__name__iexact=ADMIN_ROLE)
                    | Q(extra_roles__name__iexact=ADMIN_ROLE)
                    | Q(is_staff=True)
                    | Q(is_superuser=True)
                )
                .values_list('id', flat=True)
            )
            holders = [
                {
                    'id': u.pk,
                    'username': u.get_username(),
                    'name': (u.name or '').strip() or u.get_username(),
                    'phone': (getattr(u, 'phone', '') or '').strip(),
                }
                for u in User.objects.filter(is_active=True).only(
                    'id', 'username', 'name', 'phone', 'extra_pages')
                if u.pk not in admin_ids
                and PAYMENTS_VERIFY in (u.extra_pages or [])
            ]
            # `context` is a plain dict when the serializer is built without
            # one, so guard rather than assume it is writable.
            if isinstance(self.context, dict):
                self.context['_verifier_cache'] = holders

        # The CREATOR is filtered per receipt, not in the cached set: the
        # server refuses their own verification, and naming them would send
        # the creator to themselves.
        return [
            {k: v for k, v in u.items() if k != 'id'}
            for u in holders
            if u['id'] != obj.created_by_id
        ]

    def get_sap_branch(self, obj):
        """{'bpl_id', 'name', 'source', 'editable'} — never raises.

        Read paths must keep working when SAP is unreachable, so a failure
        here degrades to "unknown" rather than breaking the whole response.

        SKIPPED FOR LISTS. Resolving this asks HANA which branch the settled
        invoice belongs to — one round trip PER RECEIPT. On a detail page that
        is a single call for a field the user is looking at; on a list it was
        49 calls for a field no list renders, and measured at 58 seconds and
        212 queries for 49 rows on the live data. The home dashboard and the
        tracking lists all go through here, which is why they were slow.

        `many=True` builds a ListSerializer around this one, so `self.parent`
        is the reliable signal — it is set for a list and None for a single
        object, without every call site having to remember to pass a flag.
        """
        if isinstance(self.parent, serializers.ListSerializer):
            return None

        allocations = list(obj.allocations.all())
        if allocations:
            try:
                rows = hana_queries.fetch_invoice_branches(
                    company=obj.company,
                    doc_entries=[a.sap_doc_entry for a in allocations
                                 if a.sap_doc_entry])
            except Exception as exc:                    # noqa: BLE001
                # SAP being unreachable must not break reading a payment, but
                # a coding error here would otherwise hide silently — which is
                # exactly what a bare `rows = []` did during development. The
                # message is truncated: a HANA connect failure carries a long
                # multi-line RTE dump that adds no signal beyond "SAP is down".
                logger.warning('sap_branch lookup failed for receipt %s: %s',
                               obj.pk, str(exc).splitlines()[0][:200])
                rows = []
            names = {r['bpl_name'] for r in rows if r['bpl_name']}
            ids = {r['bpl_id'] for r in rows if r['bpl_id'] is not None}
            return {
                'bpl_id': next(iter(ids)) if len(ids) == 1 else None,
                'name': next(iter(names)) if len(names) == 1 else '',
                # Named so the UI can render "DELHI (Auto from Invoice)".
                'source': 'invoice',
                'editable': False,
                'conflict': len(ids) > 1,
            }
        return {
            'bpl_id': obj.sap_branch_id,
            'name': obj.sap_branch_name,
            'source': 'user' if obj.sap_branch_id else 'none',
            'editable': True,
            'conflict': False,
        }

    def get_approval(self, obj):
        """Where this document stands in its approval, for the clients.

        Reads payments' OWN flow (payments/models.py) — the engine keeps no
        runtime. `stage_label` is rendered from the flow's position rather than
        stored, and the rejection reason comes from this module's append-only
        history, so it survives configuration changes.
        """
        flow = getattr(obj, 'flow', None)
        if flow is None:
            return None

        # Prefetched by the LIST querysets (`views._rejection_prefetch`), which
        # is what keeps this off the per-row path. The detail views do not
        # prefetch, so the query below remains as the fallback — absence of the
        # attribute is the signal, not an empty list, which means "prefetched,
        # and there were no rejections".
        rejections = getattr(obj, '_rejections', None)
        if rejections is not None:
            rejection = rejections[0] if rejections else None
        else:
            from django.contrib.contenttypes.models import ContentType

            from .models import PaymentStatusHistory

            rejection = (PaymentStatusHistory.objects
                         .filter(content_type=ContentType.objects.get_for_model(
                             obj.__class__),
                             object_id=obj.pk,
                             action=PaymentStatusHistory.Action.REJECTED)
                         .order_by('-created_at')
                         .first())
        stage_sequence = None
        if flow.current_stage_id:
            stage_sequence = getattr(flow.current_stage, 'sequence', None)

        from . import workflow_flow

        payload = {
            'id': flow.id,
            'status': flow.status,
            # WHICH BUSINESS WORKFLOW THIS IS. Receipts and deposits are two
            # separate workflows under one module, and a client that has to
            # infer the difference from a document number prefix will get it
            # wrong the first time a number is entered by hand.
            'document_kind': workflow_flow.kind_of(obj),
            'current_stage': flow.current_stage_id,
            'current_stage_sequence': stage_sequence,
            'total_stage': flow.total_stage,
            'stage_label': (f'Stage {stage_sequence} of {flow.total_stage}'
                            if stage_sequence else ''),
            'rejection_reason': (rejection.reason or '') if rejection else '',
            'rejected_by': ((rejection.changed_by_username or '')
                            if rejection else ''),
            'rejected_at': rejection.created_at if rejection else None,
        }

        # THE LADDER, ON DETAIL VIEWS ONLY. Building it costs a history read
        # and a name lookup per document, which a list of fifty would pay fifty
        # times over for something no table row shows. The list already carries
        # `stage_label`, which is what a row needs.
        if self.context.get('include_stages'):
            payload['stages'] = workflow_flow.stage_progress(obj, flow)
            current = next((s for s in payload['stages']
                            if s['state'] == 'CURRENT'), None)
            payload['current_approver'] = current['approver'] if current else ''
            payload['current_approver_name'] = (
                current['approver_name'] if current else '')

        return payload


#: Columns a resolved receiving account fills in on a method entry. Listed once
#: so a tender that resolves nothing clears every one of them rather than
#: leaving half of a previous account behind on an edit.
RECEIVING_ACCOUNT_FIELDS = ('account_key', 'gl_account', 'bank_code',
                            'receiving_bank_name', 'account_number', 'branch')

_BLANK_RECEIVING_ACCOUNT = {field: '' for field in RECEIVING_ACCOUNT_FIELDS}


def resolve_receiving_account(company, method, account_key):
    """The account `account_key` names, as the snapshot to store on the entry.

    The client sends a KEY and nothing else. Everything stored is read back
    from the company's own SAP list, which is what makes three separate rules
    hold at once without trusting the caller:

      * company isolation — another company's account is not in this company's
        list, so it cannot resolve;
      * method agreement — cash keys resolve only against the cash list and
        bank keys only against the house-bank list, so CASH cannot be pointed
        at a bank account or vice versa;
      * availability — a frozen or non-postable account is absent from the
        list, so a retired drawer refuses exactly like one that never existed.

    CHEQUE and UPI share one list deliberately: SAP draws no distinction, and
    the same house bank legitimately receives both.

    Returns a dict of snapshot values, or None when no key was sent (a client
    released before the account picker). Raises DRF ValidationError when a key
    was sent and could not be resolved — silently dropping it would post the
    money somewhere the user did not choose.
    """
    key = (account_key or '').strip()
    if not key:
        return None

    is_cash = method == PaymentMethodEntry.Method.CASH
    try:
        if is_cash:
            account = bank_master.find_cash_account(company, key)
        else:
            # EXACT key only — never find_bank, whose G/L and bare-bank-code
            # fallbacks would let "INB" silently choose one of several
            # accounts. Deposits keep find_bank; this path must not.
            account = bank_master.find_bank_exact(company, key)
    except bank_master.BankMasterUnavailable as error:
        # Refuse rather than fall through to an unverified account: the money
        # is about to be told where to go.
        raise serializers.ValidationError({'methods': str(error)})
    except DjangoValidationError as error:
        message = getattr(error, 'messages', None) or [str(error)]
        raise serializers.ValidationError({'methods': message[0]})

    if not account:
        raise serializers.ValidationError(
            {'methods': f"'{key}' is not a valid receiving account for "
                        f'{company}.'})

    if is_cash:
        # A drawer has no house bank, so the bank columns stay blank — they are
        # not unknown, they do not apply.
        return {**_BLANK_RECEIVING_ACCOUNT,
                'account_key': account['gl_account'],
                'gl_account': account['gl_account'],
                'receiving_bank_name': account.get('account_name', ''),
                }

    return {'account_key': account['key'],
            'gl_account': account['gl_account'],
            'bank_code': account['bank_code'],
            'receiving_bank_name': account['display_name'],
            'account_number': account.get('account_number', ''),
            'branch': account.get('branch', '')}


class PaymentReceiptCreateSerializer(serializers.ModelSerializer):
    """Create a receipt with its methods, denominations and allocations in one
    atomic call."""

    methods = PaymentMethodEntrySerializer(many=True)
    allocations = PaymentAllocationSerializer(many=True, required=False)

    class Meta:
        model = PaymentReceipt
        fields = ['id', 'company', 'card_code', 'card_name',
                  'received_from_type', 'received_from_person',
                  'received_from_name',
                  'payment_date', 'is_advance', 'currency', 'remarks',
                  'sap_branch_id',
                  'methods', 'allocations']
        # Derived in validate() from the chosen person, never accepted from the
        # client: a caller who could set the name independently of the FK could
        # put any name on a receipt pointing at someone else.
        read_only_fields = ['received_from_name']

    def validate(self, attrs):
        """Cross-field rules, for both a create and a partial update.

        On a PATCH the payload carries only what changed, so every field is read
        as "the incoming value, else the one already stored". Reading `attrs`
        alone would treat an omitted `is_advance` as False and reject an edit
        that never touched it.
        """
        instance = self.instance

        def current(field, default=None):
            if field in attrs:
                return attrs[field]
            if instance is not None:
                return getattr(instance, field, default)
            return default

        if current('received_from_type') == PaymentReceipt.ReceivedFromType.PERSON \
                and not current('received_from_person'):
            raise serializers.ValidationError({
                'received_from_person':
                    'Required when the payment is received from a company person.'})

        # Freeze the collector's name onto the receipt.
        #
        # Keyed on `'received_from_person' in attrs` — the person was SENT —
        # rather than on the resolved value, so it fires exactly when the
        # choice is made or changed and never on an unrelated PATCH. A rename
        # of the master afterwards must not reach receipts already written, so
        # there is deliberately no other path that rewrites this.
        if 'received_from_person' in attrs:
            person = attrs['received_from_person']
            attrs['received_from_name'] = person.name if person else ''

        # Children are replaced wholesale when sent, so an omitted key means
        # "leave the stored rows alone" — validate against those instead.
        if 'methods' in attrs:
            methods = attrs['methods'] or []
            total = sum((m['amount'] for m in methods), Decimal('0'))
        else:
            methods = list(instance.methods.all()) if instance else []
            total = sum((m.amount for m in methods), Decimal('0'))
        if not methods:
            raise serializers.ValidationError(
                {'methods': 'Add at least one payment method.'})

        # ONE method per receipt — the rule Finance actually follows: a customer
        # paying by three tenders becomes three Incoming Payments in SAP, not
        # one document carrying all three.
        #
        # It is also the only structural fix for a real misposting. SAP has a
        # SINGLE TransferAccount/TransferSum pair, so a receipt mixing UPI and
        # CHEQUE had to merge them: DocEntry 20802 sent ₹12,00,000 of cheque
        # money to the UPI bank G/L because the first tender's account won.
        # With one method per receipt that merge cannot occur.
        #
        # Compared on DISTINCT method, so several cash lines on one receipt
        # (a legitimate way to record two bundles of notes) still pass.
        distinct_methods = sorted({
            m['method'] if isinstance(m, dict) else m.method for m in methods
        })
        if len(distinct_methods) > 1:
            raise serializers.ValidationError({
                'methods': (
                    'Each payment receipt must contain exactly one payment '
                    'method. Create separate receipts for CASH, UPI or '
                    f'CHEQUE. This one has: {", ".join(distinct_methods)}.'
                )})

        # Resolve the chosen receiving account against THIS receipt's company,
        # and freeze what it resolved to onto the line. Done here rather than
        # in the entry serializer because only the receipt knows the company,
        # and the company is the whole basis of the check.
        if 'methods' in attrs:
            company = current('company')
            for entry in attrs['methods'] or []:
                resolved = resolve_receiving_account(
                    company, entry.get('method'), entry.get('account_key'))
                # No key sent -> no snapshot. Blank, never a leftover: method
                # rows are replaced wholesale on an edit, and a half-filled
                # account would be worse than none.
                entry.update(resolved or _BLANK_RECEIVING_ACCOUNT)

        if 'allocations' in attrs:
            allocations = attrs['allocations'] or []
            allocated = sum(
                (a['amount_applied'] for a in allocations), Decimal('0'))
        else:
            allocations = list(instance.allocations.all()) if instance else []
            allocated = sum((a.amount_applied for a in allocations), Decimal('0'))

        if allocated > total:
            raise serializers.ValidationError({
                'allocations':
                    f'Allocated {allocated} exceeds the payment total {total}.'})
        if not current('is_advance') and not allocations:
            raise serializers.ValidationError({
                'allocations':
                    'Select at least one invoice, or mark this as an advance.'})

        self._validate_branch(attrs, current, allocations)
        return attrs

    @staticmethod
    def _validate_branch(attrs, current, allocations):
        """A branch may be chosen ONLY when there is no invoice to inherit one.

        An invoice payment must use the invoice's branch — SAP rejects any
        other — so accepting a user's branch there would store a value that is
        either ignored or wrong. Refusing it is clearer than both.
        """
        from sap_sync.models import Branch

        branch_id = attrs.get('sap_branch_id')
        if branch_id in (None, ''):
            return

        if allocations:
            raise serializers.ValidationError({'sap_branch_id': (
                'The branch of an invoice payment comes from the invoice and '
                'cannot be set here.')})

        company = current('company')
        row = (Branch.objects
               .filter(category=company, bpl_id=branch_id, is_active=True)
               .first())
        if row is None:
            raise serializers.ValidationError({'sap_branch_id': (
                f'Branch {branch_id} is not a valid SAP branch for '
                f'{company}.')})
        # Snapshot the name, so a receipt stays readable if the branch is
        # later renamed or deactivated in SAP.
        attrs['sap_branch_name'] = row.bpl_name

    @transaction.atomic
    def create(self, validated_data):
        methods = validated_data.pop('methods')
        allocations = validated_data.pop('allocations', [])
        user = self.context['request'].user
        company = validated_data['company']

        # Derived, never taken from the payload. orders/views.py:2530 trusts the
        # request for total_amount, so a partial write leaves the header
        # disagreeing with SUM(children).
        total = sum((m['amount'] for m in methods), Decimal('0'))
        allocated = sum((a['amount_applied'] for a in allocations), Decimal('0'))

        from .services import resolve_company_db

        receipt = PaymentReceipt.objects.create(
            **validated_data,
            receipt_no=next_document_number(
                doc_type='RECEIPT', company=company, prefix='RCP',
                when=validated_data.get('payment_date') or timezone.localdate()),
            company_db=resolve_company_db(company),
            total_amount=total,
            allocated_amount=allocated,
            status=PaymentReceipt.Status.DRAFT,
            created_by=user,
        )

        for entry_data in methods:
            denominations = entry_data.pop('denominations', [])
            entry = PaymentMethodEntry.objects.create(receipt=receipt, **entry_data)
            if denominations:
                CashDenomination.objects.bulk_create([
                    CashDenomination(entry=entry, **row) for row in denominations
                ])

        if allocations:
            PaymentAllocation.objects.bulk_create([
                PaymentAllocation(receipt=receipt, **row) for row in allocations
            ])

        from .services import log_status
        # CREATED, not the STATUS_CHANGED default: this is the entry point of
        # the payment's life and reads as such in the timeline.
        log_status(receipt, to_status=receipt.status, user=user,
                   action=PaymentStatusHistory.Action.CREATED,
                   reason='Receipt created.')

        # Hand off to the verifiers. Inside this atomic block, so the
        # notifications commit with the receipt and roll back with it — nobody
        # is ever told to verify something that failed to save. Failures are
        # isolated by the framework and never break the create.
        if receipt.verification_status == PaymentReceipt.VerificationStatus.PENDING:
            from .notification_events import publish_receipt_verification_required
            publish_receipt_verification_required(receipt, user)
        return receipt

    @transaction.atomic
    def update(self, instance, validated_data):
        """Replace the receipt's editable content in one atomic write.

        Children are REPLACED, not merged: the client sends the complete set it
        wants, and diffing method rows by index would silently re-map a cheque's
        details onto a cash line when the user deletes one in the middle. The
        old rows go, the new ones land, and the totals are recomputed from them.

        Deliberately NOT editable: `receipt_no` (issued once, quoted elsewhere),
        `company` (it decides the SAP database and the party's identity — a
        change there means a different document), `status`, and every SAP field.
        """
        methods = validated_data.pop('methods', None)
        allocations = validated_data.pop('allocations', None)
        user = self.context['request'].user

        # Snapshot the MATERIAL figures before anything is overwritten, so the
        # comparison below is against what was actually verified.
        was_verified = (instance.verification_status
                        == PaymentReceipt.VerificationStatus.VERIFIED)
        before = self._material_snapshot(instance) if was_verified else None
        # Taken UNCONDITIONALLY, unlike the material snapshot above: every edit
        # gets a change log, not only edits to a verified receipt.
        audit_before = self._audit_snapshot(instance)

        for field, value in validated_data.items():
            setattr(instance, field, value)

        if methods is not None:
            instance.methods.all().delete()          # cascades denominations
            for entry_data in methods:
                denominations = entry_data.pop('denominations', [])
                entry = PaymentMethodEntry.objects.create(
                    receipt=instance, **entry_data)
                if denominations:
                    CashDenomination.objects.bulk_create([
                        CashDenomination(entry=entry, **row)
                        for row in denominations
                    ])
            instance.total_amount = sum(
                (m['amount'] for m in methods), Decimal('0'))

        if allocations is not None:
            instance.allocations.all().delete()
            if allocations:
                PaymentAllocation.objects.bulk_create([
                    PaymentAllocation(receipt=instance, **row)
                    for row in allocations
                ])
            instance.allocated_amount = sum(
                (a['amount_applied'] for a in allocations), Decimal('0'))

        instance.save()

        # A MATERIAL edit after verification invalidates it. Otherwise the
        # record would claim someone verified figures they never saw: the
        # cheque number, the amount or the allocations could all change while
        # `verified_by` still names the person who checked the old ones.
        #
        # Deliberately narrow. Re-reading `instance` from the database is what
        # makes this honest — the child rows were replaced above, so the
        # snapshot has to come from the saved state, not the in-memory copy.
        # A remarks or branch edit is NOT material and leaves verification
        # intact; re-verifying for a typo fix would make the gate a nuisance
        # and train people to click through it.
        # Re-read before either comparison. The method and allocation rows
        # were deleted and recreated above, so the in-memory instance still
        # holds the OLD children in its prefetch cache and would report no
        # change at all.
        instance.refresh_from_db()
        audit_after = self._audit_snapshot(instance)

        reset_verification = False
        if was_verified:
            if self._material_snapshot(instance) != before:
                instance.verification_status = (
                    PaymentReceipt.VerificationStatus.PENDING)
                instance.verified_by = None
                instance.verified_at = None
                instance.save(update_fields=[
                    'verification_status', 'verified_by', 'verified_at',
                    'updated_at'])
                reset_verification = True

        from .services import log_status
        # UPDATED, not STATUS_CHANGED: an edit changes the figures without
        # changing the status, and the timeline must say WHO altered a
        # document — an approver correcting a SAP-rejected receipt is a
        # different event from the status moving on its own.
        log_status(instance, from_status=instance.status,
                   to_status=instance.status, user=user,
                   action=PaymentStatusHistory.Action.UPDATED,
                   change_data=self._diff(audit_before, audit_after),
                   reason=('Receipt edited — verification reset, the amounts '
                           'changed after it was verified.'
                           if reset_verification else 'Receipt edited.'))
        return instance

    @staticmethod
    def _audit_snapshot(instance):
        """The editable business fields, JSON-safe, for the change log.

        SEPARATE from `_material_snapshot` on purpose. That one answers "did
        the figures a verifier checked change?" and holds tuples of Decimals
        because it only ever needs `==`. This one is written into a JSON
        column and read by a person, so every value is a string and the shape
        is stable.

        Only fields a user can actually edit through the form. Deliberately
        absent: `created_at`/`updated_at` (not edits), SAP responses and
        DocEntry (written by the poster, not the user), attachments (large,
        and their own audit), and anything identifying — no IPs, no headers,
        no tokens.
        """
        def money(value):
            # str(Decimal) keeps the scale the database stores. float() would
            # render 1000.00 as 999.9999999999999 in an audit record.
            return str(value) if value is not None else None

        methods = list(instance.methods.all())
        # One method per receipt is the business rule, so the tender is a
        # single value rather than a list — a list would read as though a
        # receipt could mix cash and cheque.
        first = methods[0] if methods else None

        return {
            'amount': money(instance.total_amount),
            'payment_date': (instance.payment_date.isoformat()
                             if instance.payment_date else None),
            'is_advance': instance.is_advance,
            'remarks': instance.remarks or '',
            'received_from': instance.received_from_name or '',
            'payment_method': first.method if first else None,
            'upi_reference': (first.upi_reference or '') if first else '',
            'cheque_number': (first.cheque_number or '') if first else '',
            'cheque_bank': (first.bank_name or '') if first else '',
            'cheque_date': (first.cheque_date.isoformat()
                            if first and first.cheque_date else None),
            # Sorted by the invoice key so re-sending the same rows in a
            # different order is not reported as a change.
            'allocations': sorted(
                ({'invoice': a.sap_doc_num or a.sap_doc_entry,
                  'amount': money(a.amount_applied)}
                 for a in instance.allocations.all()),
                key=lambda row: str(row['invoice']),
            ),
        }

    @staticmethod
    def _diff(before, after):
        """{field: {'old': ..., 'new': ...}} for the fields that changed.

        None when nothing did: an UPDATED row with an empty object would claim
        an edit it cannot describe, and `{}` reads as "we did not look".
        """
        if not before:
            return None
        changed = {
            key: {'old': before.get(key), 'new': after.get(key)}
            for key in after
            if before.get(key) != after.get(key)
        }
        return changed or None

    @staticmethod
    def _material_snapshot(instance):
        """The figures a verifier physically checks, as a comparable value.

        Amount, tender and what the money settles — plus the cheque/UPI
        identifiers that tie the entry to a specific physical instrument.
        Sorted, so re-sending the same rows in a different order is not
        mistaken for a change.
        """
        return {
            'total_amount': instance.total_amount,
            'allocated_amount': instance.allocated_amount,
            'methods': sorted(
                (m.method, m.amount, m.cheque_number or '',
                 m.bank_name or '', m.cheque_date, m.upi_reference or '')
                for m in instance.methods.all()
            ),
            'allocations': sorted(
                (a.sap_doc_entry, a.invoice_type or '', a.amount_applied)
                for a in instance.allocations.all()
            ),
        }


# ---------------------------------------------------------------------------
# Deposit
# ---------------------------------------------------------------------------

class BankDepositLineSerializer(serializers.ModelSerializer):
    """One banked receipt, with enough of the receipt to verify it.

    An approver signing off a deposit is confirming that specific notes and
    cheques were handed over. Answering "which of these is the cheque, and is
    it the one in my hand?" previously meant opening each receipt separately,
    so the fields that identify the money are included here.
    """

    receipt_no = serializers.CharField(source='receipt.receipt_no', read_only=True)
    card_name = serializers.CharField(source='receipt.card_name', read_only=True)
    card_code = serializers.CharField(source='receipt.card_code', read_only=True)
    payment_date = serializers.DateField(
        source='receipt.payment_date', read_only=True)
    receipt_status = serializers.CharField(
        source='receipt.status', read_only=True)
    receipt_total = serializers.DecimalField(
        source='receipt.total_amount', max_digits=15, decimal_places=2,
        read_only=True)
    receipt_remarks = serializers.CharField(
        source='receipt.remarks', read_only=True)
    collected_by = serializers.CharField(
        source='receipt.received_from_name', read_only=True, default='')
    # When the RECEIPT reached SAP. Paired with the deposit's own date, this is
    # how long the money sat in hand between being booked and being banked —
    # the question "we took this on the 1st, why was it banked on the 9th?".
    # Null until the receipt posts, and on receipts that never did.
    receipt_posted_at = serializers.DateTimeField(
        source='receipt.sap_posted_at', read_only=True)
    # WHERE THE RECEIPT LANDED IN SAP.
    #
    # A deposit posts its CASH share and nothing else: a cheque reached the
    # bank when its OWN receipt posted, so re-posting it here would debit the
    # bank twice. That is correct accounting and completely invisible to the
    # approver, who sees a deposit for the full amount and one SAP document
    # covering only part of it.
    #
    # This is the missing half of that story — the document number the cheque
    # is actually recorded under — so the detail screen can say where each
    # tender ended up instead of leaving the cheques unaccounted for.
    receipt_sap_doc_num = serializers.IntegerField(
        source='receipt.sap_doc_num', read_only=True)
    receipt_sap_doc_entry = serializers.IntegerField(
        source='receipt.sap_doc_entry', read_only=True)
    # Cash / cheque lines, each with its cheque number, payer bank and date.
    methods = PaymentMethodEntrySerializer(
        source='receipt.methods', many=True, read_only=True)

    class Meta:
        model = BankDepositLine
        fields = ['id', 'receipt', 'receipt_no', 'card_name', 'card_code',
                  'payment_date', 'receipt_posted_at', 'receipt_sap_doc_num',
                  'receipt_sap_doc_entry', 'receipt_status',
                  'receipt_total', 'receipt_remarks', 'collected_by',
                  'methods', 'amount']


class BankDepositSerializer(serializers.ModelSerializer):
    lines = BankDepositLineSerializer(many=True, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    bank_account_name = serializers.CharField(
        source='bank_display_name', read_only=True)
    deposited_by_name = serializers.CharField(
        source='deposited_by.name', read_only=True, default='')
    shortfall = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True)
    approval = serializers.SerializerMethodField()
    source_gl_account = serializers.SerializerMethodField()
    # `deposited_by` is the person who physically banked the cash; `created_by`
    # is the user who raised the entry in OMS. They are frequently different.
    created_by_name = serializers.CharField(
        source='created_by.name', read_only=True, default='')
    created_by_username = serializers.CharField(
        source='created_by.username', read_only=True, default='')

    class Meta:
        model = BankDeposit
        fields = ['id', 'deposit_no', 'company', 'deposit_date',
                  'deposited_by', 'deposited_by_name',
                  'bank_key', 'bank_code', 'bank_gl_account',
                  'bank_account_name', 'source_gl_account', 'deposit_type',
                  'collected_amount', 'deposit_amount', 'shortfall',
                  'shortfall_reason', 'bank_charge', 'currency',
                  'slip_number', 'remarks', 'status', 'status_display',
                  'sap_doc_entry', 'sap_doc_num', 'sap_trans_id',
                  'sap_posted_at',
                  'sap_response', 'sap_raw_error', 'sap_raw_error_code',
                  # SAP-side cancellation, kept apart from sap_response so the
                  # original posting confirmation stays readable.
                  'sap_cancelled_at', 'sap_cancellation_response',
                  'sap_reconciled_at',
                  'lines', 'attachments', 'approval',
                  'created_by', 'created_by_name', 'created_by_username',
                  'created_at', 'updated_at']
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_source_gl_account(self, obj):
        """The G/L this deposit EMPTIES — the other half of the movement.

        A deposit moves money between two accounts: out of the cash/collection
        G/L and into the bank G/L. `bank_gl_account` is only the destination,
        so a reader could see where the money landed but not where it came
        from — and `bank_display_name` already carries the destination number,
        which made the header repeat one side twice and never show the other.

        STORED FIRST. A deposit submitted from this phase onward froze the
        drawer its receipts were received into, and that is a per-document
        fact: reading it back from configuration would let a later mapping
        edit rewrite what an old deposit says it emptied.

        The live lookup remains only for a deposit that has no stored value —
        one submitted before this phase, or still a draft — which is the value
        it would have posted then. Returns None when there is nothing to show,
        so the UI omits the row instead of printing a blank account number.
        """
        stored = (getattr(obj, 'source_gl_account', '') or '').strip()
        if stored:
            return stored

        # Nothing to fall back to any more: the account a deposit empties is
        # frozen on the deposit at submit, and deposits raised before that had
        # theirs written from their own receipts. A blank here means the
        # deposit genuinely records no drawer — a cheque-only deposit, which
        # empties none.
        return None

    def get_approval(self, obj):
        """Where this document stands in its approval, for the clients.

        Reads payments' OWN flow (payments/models.py) — the engine keeps no
        runtime. `stage_label` is rendered from the flow's position rather than
        stored, and the rejection reason comes from this module's append-only
        history, so it survives configuration changes.
        """
        flow = getattr(obj, 'flow', None)
        if flow is None:
            return None

        # Prefetched by the LIST querysets (`views._rejection_prefetch`), which
        # is what keeps this off the per-row path. The detail views do not
        # prefetch, so the query below remains as the fallback — absence of the
        # attribute is the signal, not an empty list, which means "prefetched,
        # and there were no rejections".
        rejections = getattr(obj, '_rejections', None)
        if rejections is not None:
            rejection = rejections[0] if rejections else None
        else:
            from django.contrib.contenttypes.models import ContentType

            from .models import PaymentStatusHistory

            rejection = (PaymentStatusHistory.objects
                         .filter(content_type=ContentType.objects.get_for_model(
                             obj.__class__),
                             object_id=obj.pk,
                             action=PaymentStatusHistory.Action.REJECTED)
                         .order_by('-created_at')
                         .first())
        stage_sequence = None
        if flow.current_stage_id:
            stage_sequence = getattr(flow.current_stage, 'sequence', None)

        from . import workflow_flow

        payload = {
            'id': flow.id,
            'status': flow.status,
            # WHICH BUSINESS WORKFLOW THIS IS. Receipts and deposits are two
            # separate workflows under one module, and a client that has to
            # infer the difference from a document number prefix will get it
            # wrong the first time a number is entered by hand.
            'document_kind': workflow_flow.kind_of(obj),
            'current_stage': flow.current_stage_id,
            'current_stage_sequence': stage_sequence,
            'total_stage': flow.total_stage,
            'stage_label': (f'Stage {stage_sequence} of {flow.total_stage}'
                            if stage_sequence else ''),
            'rejection_reason': (rejection.reason or '') if rejection else '',
            'rejected_by': ((rejection.changed_by_username or '')
                            if rejection else ''),
            'rejected_at': rejection.created_at if rejection else None,
        }

        # THE LADDER, ON DETAIL VIEWS ONLY. Building it costs a history read
        # and a name lookup per document, which a list of fifty would pay fifty
        # times over for something no table row shows. The list already carries
        # `stage_label`, which is what a row needs.
        if self.context.get('include_stages'):
            payload['stages'] = workflow_flow.stage_progress(obj, flow)
            current = next((s for s in payload['stages']
                            if s['state'] == 'CURRENT'), None)
            payload['current_approver'] = current['approver'] if current else ''
            payload['current_approver_name'] = (
                current['approver_name'] if current else '')

        return payload


class BankDepositCreateSerializer(serializers.ModelSerializer):
    """Create a deposit from a set of posted receipts."""

    receipt_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, allow_empty=False)

    class Meta:
        model = BankDeposit
        fields = ['id', 'company', 'deposit_date', 'deposited_by',
                  'bank_key', 'deposit_type', 'deposit_amount',
                  'shortfall_reason', 'bank_charge', 'currency',
                  'slip_number', 'remarks', 'receipt_ids']

    def validate(self, attrs):
        """Cross-field rules, for both a create and a partial update.

        On a PATCH the payload carries only what changed, so each value is read
        as "the incoming one, else the one already stored". Reading `attrs`
        alone would treat an omitted field as absent and reject an edit that
        never touched it.
        """
        instance = self.instance

        def current(field, default=None):
            if field in attrs:
                return attrs[field]
            if instance is not None:
                return getattr(instance, field, default)
            return default

        company = current('company')
        if 'receipt_ids' in attrs:
            receipt_ids = attrs['receipt_ids']
        elif instance is not None:
            # Unchanged: keep the receipts already banked here.
            receipt_ids = list(
                instance.lines.values_list('receipt_id', flat=True))
        else:
            receipt_ids = []

        # `methods` is prefetched because the cash total walks every entry of
        # every receipt; without it each selected receipt costs a query.
        receipts = PaymentReceipt.objects.filter(
            id__in=receipt_ids, company=company).prefetch_related('methods')
        if receipts.count() != len(set(receipt_ids)):
            raise serializers.ValidationError({
                'receipt_ids':
                    'One or more payments do not exist in the selected company.'})

        # Banked already? The answer lives in BankDepositLine, which is the only
        # written link and holds the unique constraint on `receipt` that makes
        # double-banking impossible at the database level.
        #
        # A receipt already on THIS deposit is excluded: on an edit it is not a
        # double-banking, it is the row being kept.
        already_qs = receipts.filter(deposit_lines__isnull=False)
        if instance is not None:
            already_qs = already_qs.exclude(deposit_lines__deposit=instance)
        already = list(already_qs.values_list('receipt_no', flat=True))
        if already:
            raise serializers.ValidationError({
                'receipt_ids': f'Already deposited: {", ".join(already)}.'})

        # CASH ONLY — see services.cash_total_for_receipts. A cheque in these
        # receipts is already in SAP under the receipt that took it, so it is
        # carried as a record of the day it was handed in, not as an amount.
        collected = payment_services.cash_total_for_receipts(receipts)
        deposit_amount = current('deposit_amount')
        if deposit_amount > collected:
            raise serializers.ValidationError({
                'deposit_amount':
                    f'Cannot bank {deposit_amount}; these payments hold only '
                    f'{collected} in cash.'})
        if deposit_amount < collected and not (
                current('shortfall_reason') or '').strip():
            raise serializers.ValidationError({
                'shortfall_reason':
                    'A reason is required when depositing less than collected.'})

        # Resolve the chosen bank against SAP and snapshot it. The user picks
        # a bank; the G/L is never typed. Verified here so a bank removed from
        # SAP is caught at entry rather than at posting.
        try:
            bank = bank_master.find_bank(company,
                                         current('bank_key') or '')
        except bank_master.BankMasterUnavailable as exc:
            # A draft may still be saved; submit re-checks and blocks.
            bank = None
            if not self.partial:
                logger.warning('deposit bank unverified: %s', exc)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'bank_key': exc.messages}) from exc
        if bank is not None:
            attrs['bank_key'] = bank['key']
            attrs['bank_code'] = bank['bank_code']
            attrs['bank_gl_account'] = bank['gl_account']
            attrs['bank_display_name'] = bank['label']

        # One cash drawer per deposit. Refused while the user is still picking
        # receipts, rather than at submit, because the fix is to change the
        # SELECTION. The same rule is enforced again in validate_deposit, so
        # no other path can reach SAP with two sources.
        try:
            payment_services.check_one_cash_source(
                payment_services.cash_sources_for_receipts(
                    company, receipts))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                {'receipt_ids': exc.messages}) from exc

        attrs['_receipts'] = list(receipts)
        attrs['_collected'] = collected
        # DERIVED, never taken from the client. The old picker let a user tag
        # a pure-cash deposit as MIXED, and most of them did — the label said
        # nothing about the contents. It now follows the cash.
        attrs['deposit_type'] = payment_services.derive_deposit_type(collected)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop('receipt_ids')
        receipts = validated_data.pop('_receipts')
        collected = validated_data.pop('_collected')
        user = self.context['request'].user
        company = validated_data['company']

        from .services import log_status, resolve_company_db

        deposit = BankDeposit.objects.create(
            **validated_data,
            deposit_no=next_document_number(
                doc_type='DEPOSIT', company=company, prefix='DEP',
                when=validated_data.get('deposit_date') or timezone.localdate()),
            company_db=resolve_company_db(company),
            collected_amount=collected,
            status=BankDeposit.Status.DRAFT,
            created_by=user,
        )
        # Creating the lines IS claiming the receipts: the unique constraint on
        # BankDepositLine.receipt makes it impossible for a second deposit to
        # take one, so no separate claim write is needed.
        BankDepositLine.objects.bulk_create([
            BankDepositLine(deposit=deposit, receipt=r, amount=r.total_amount)
            for r in receipts
        ])

        log_status(deposit, to_status=deposit.status, user=user,
                   reason='Deposit created.')
        return deposit

    @transaction.atomic
    def update(self, instance, validated_data):
        """Edit a deposit in place.

        Used to correct a deposit SAP refused: the approver or creator fixes
        the bank, the amount or the receipts and sends it round again. The
        deposit NUMBER never changes — it is the same physical hand-over, and a
        new number would break the link to whatever has already referenced it.

        Receipt lines are replaced wholesale when `receipt_ids` is sent, and
        left untouched when it is not. Partial means partial.
        """
        validated_data.pop('receipt_ids', None)
        receipts = validated_data.pop('_receipts', None)
        collected = validated_data.pop('_collected', None)
        user = self.context['request'].user

        from .services import log_status

        audit_before = self._audit_snapshot(instance)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        if collected is not None:
            instance.collected_amount = collected
        instance.save()

        if receipts is not None:
            # Delete-then-recreate rather than diffing: the unique constraint on
            # BankDepositLine.receipt means a receipt dropped from this deposit
            # must release its claim before another deposit can take it, and one
            # atomic replace is easier to reason about than a partial diff.
            instance.lines.all().delete()
            BankDepositLine.objects.bulk_create([
                BankDepositLine(deposit=instance, receipt=r,
                                amount=r.total_amount)
                for r in receipts
            ])

        # Re-read before diffing: the lines were deleted and recreated above,
        # so the in-memory instance still holds the OLD ones in its prefetch
        # cache and would report no change at all.
        instance.refresh_from_db()

        # Same reasoning as the receipt edit above: record WHO changed it,
        # under an action that reads as an edit rather than a status move —
        # and WHAT they changed, so an approver reviewing a corrected deposit
        # does not have to guess which figure moved.
        log_status(instance, from_status=instance.status,
                   to_status=instance.status, user=user,
                   action=PaymentStatusHistory.Action.UPDATED,
                   change_data=PaymentReceiptCreateSerializer._diff(
                       audit_before, self._audit_snapshot(instance)),
                   reason='Deposit updated.')
        return instance

    @staticmethod
    def _audit_snapshot(instance):
        """The editable business fields of a deposit, JSON-safe.

        The deposit twin of `PaymentReceiptCreateSerializer._audit_snapshot`,
        and it follows the same two rules: every value is a string so the JSON
        column holds no floats, and only fields a user can actually edit
        through the form appear. SAP columns, timestamps and the deposit number
        are all absent — none of them is something a person changed.
        """
        def money(value):
            # str(Decimal) keeps the stored scale; float() would render
            # 1000.00 as 999.9999999999999 in an audit record.
            return str(value) if value is not None else None

        return {
            'deposit_date': (instance.deposit_date.isoformat()
                             if instance.deposit_date else None),
            'deposit_amount': money(instance.deposit_amount),
            'collected_amount': money(instance.collected_amount),
            'bank': instance.bank_display_name or instance.bank_code or '',
            'slip_number': instance.slip_number or '',
            'shortfall_reason': instance.shortfall_reason or '',
            'remarks': instance.remarks or '',
            'deposited_by': (instance.deposited_by.name
                             if instance.deposited_by_id else ''),
            # Sorted by receipt number so re-sending the same set in a
            # different order is not reported as a change.
            'receipts': sorted(
                line.receipt.receipt_no for line in instance.lines.all()
            ),
        }
