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
    PaymentMethodMapping,
    PaymentReceipt,
    PaymentStatusHistory,
    SapCompanyMap,
)


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

class SapCompanyMapSerializer(serializers.ModelSerializer):
    """Company -> SAP database mapping.

    `company_db` and `hana_schema` were previously omitted, so the admin UI
    rendered blank columns for the two fields that actually matter — and there
    was no way to see or fix a mapping outside Django admin.

    `cash_gl_account` lives here rather than in the payment-method mapping for
    a physical reason: every other tender lands in a BANK, and SAP publishes
    those as House Bank Accounts (DSC1). Cash lands in a drawer, which is not a
    bank and has no DSC1 row — so there is nothing to pick from and the G/L has
    to be named directly.

    `deposit_source_gl_account` is the account a DEPOSIT credits. It is a
    second field rather than a reuse of `cash_gl_account` because SAP
    validates the two roles differently: a receipt's CashAccount must be a
    cash-flow account (OACT.Finanse='Y'), a deposit's CardCode must not be.
    """

    class Meta:
        model = SapCompanyMap
        fields = ['id', 'company', 'display_name', 'company_db', 'hana_schema',
                  'default_bpl_id', 'cash_gl_account',
                  'deposit_source_gl_account', 'is_active',
                  'sort_order']

    def validate_company(self, value):
        """`company` is unique — report the clash as a field error, not a 500."""
        qs = SapCompanyMap.objects.filter(company=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                f'A mapping for {value} already exists. Edit that one instead.')
        return value


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
                  'deposit_account']
        read_only_fields = ['sap_check_key']

    def get_deposit_account(self, obj):
        """Our account for this tender, or None when not configured."""
        company = getattr(obj.receipt, 'company', None)
        if not company:
            return None
        if obj.method == PaymentMethodEntry.Method.CASH:
            resolver = bank_master.PaymentAccountResolver(company)
            gl = resolver.cash_gl()
            return ({'bank_name': 'Cash', 'gl_account': gl,
                     'account_number': '', 'branch': ''} if gl else None)
        try:
            found = bank_master.PaymentAccountResolver(company).resolve(
                obj.method)
        except Exception:                                   # noqa: BLE001
            # Never let a SAP outage break reading a payment.
            return None
        if not found:
            return None
        return {'bank_name': found['bank_name'],
                'gl_account': found['gl_account'],
                'account_number': found['account_number'],
                'branch': found['branch']}

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


class PaymentStatusHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentStatusHistory
        fields = ['id', 'from_status', 'to_status', 'reason', 'actor_kind',
                  'changed_by_username', 'created_at']


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

class PaymentReceiptSerializer(serializers.ModelSerializer):
    """Read shape for lists and detail."""

    methods = PaymentMethodEntrySerializer(many=True, read_only=True)
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    received_from_name = serializers.CharField(
        source='received_from_person.name', read_only=True, default='')
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
                  'created_at', 'updated_at']
        read_only_fields = fields

    def get_sap_branch(self, obj):
        """{'bpl_id', 'name', 'source', 'editable'} — never raises.

        Read paths must keep working when SAP is unreachable, so a failure
        here degrades to "unknown" rather than breaking the whole response.
        """
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
        request = obj.approvals.order_by('-created_at').first()
        if not request:
            return None
        # The last REJECT of the CURRENT round, so a creator opening a rejected
        # entry sees why without hunting through the approval history. Scoped to
        # the round: an older round's rejection was already acted on.
        rejection = (request.actions
                     .filter(action='REJECT', round_number=request.round_number)
                     .order_by('-sequence').first())
        return {
            'id': request.id,
            'status': request.status,
            'current_level': request.current_level,
            'total_levels': request.total_levels,
            'level_label': request.level_label,
            'round_number': request.round_number,
            'rejection_reason': (rejection.remarks or '') if rejection else '',
            'rejected_by': (rejection.approver_username or '') if rejection else '',
            'rejected_at': rejection.acted_at if rejection else None,
        }


class PaymentReceiptCreateSerializer(serializers.ModelSerializer):
    """Create a receipt with its methods, denominations and allocations in one
    atomic call."""

    methods = PaymentMethodEntrySerializer(many=True)
    allocations = PaymentAllocationSerializer(many=True, required=False)

    class Meta:
        model = PaymentReceipt
        fields = ['id', 'company', 'card_code', 'card_name',
                  'received_from_type', 'received_from_person',
                  'payment_date', 'is_advance', 'currency', 'remarks',
                  'sap_branch_id',
                  'methods', 'allocations']

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
        log_status(receipt, to_status=receipt.status, user=user,
                   reason='Receipt created.')
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

        from .services import log_status
        # UPDATED, not STATUS_CHANGED: an edit changes the figures without
        # changing the status, and the timeline must say WHO altered a
        # document — an approver correcting a SAP-rejected receipt is a
        # different event from the status moving on its own.
        log_status(instance, from_status=instance.status,
                   to_status=instance.status, user=user,
                   action=PaymentStatusHistory.Action.UPDATED,
                   reason='Receipt edited.')
        return instance


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
        source='receipt.received_from_person.name', read_only=True, default='')
    # Cash / cheque lines, each with its cheque number, payer bank and date.
    methods = PaymentMethodEntrySerializer(
        source='receipt.methods', many=True, read_only=True)

    class Meta:
        model = BankDepositLine
        fields = ['id', 'receipt', 'receipt_no', 'card_name', 'card_code',
                  'payment_date', 'receipt_status', 'receipt_total',
                  'receipt_remarks', 'collected_by', 'methods', 'amount']


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
                  'bank_account_name', 'deposit_type',
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

    def get_approval(self, obj):
        request = obj.approvals.order_by('-created_at').first()
        if not request:
            return None
        # The last REJECT of the CURRENT round, so a creator opening a rejected
        # entry sees why without hunting through the approval history. Scoped to
        # the round: an older round's rejection was already acted on.
        rejection = (request.actions
                     .filter(action='REJECT', round_number=request.round_number)
                     .order_by('-sequence').first())
        return {
            'id': request.id,
            'status': request.status,
            'current_level': request.current_level,
            'total_levels': request.total_levels,
            'level_label': request.level_label,
            'round_number': request.round_number,
            'rejection_reason': (rejection.remarks or '') if rejection else '',
            'rejected_by': (rejection.approver_username or '') if rejection else '',
            'rejected_at': rejection.acted_at if rejection else None,
        }


class PaymentMethodMappingSerializer(serializers.ModelSerializer):
    """Admin CRUD for the method -> SAP account mapping.

    The resolved SAP fields are read-only extras so the admin table can show
    the bank, G/L and account number without a second request — and so a
    mapping whose account has vanished from SAP shows up as invalid rather
    than as a plausible-looking row.
    """

    resolved = serializers.SerializerMethodField()

    class Meta:
        model = PaymentMethodMapping
        fields = ['id', 'company', 'payment_method', 'bank_key', 'priority',
                  'is_active', 'resolved', 'created_at', 'updated_at']
        # The partial unique constraint reports "must make a unique set",
        # naming neither the method nor the row already holding it. validate()
        # below owns the message; the DB still backs it up.
        validators = []

    def get_resolved(self, obj):
        try:
            found = bank_master.find_bank(obj.company, obj.bank_key)
        except (bank_master.BankMasterUnavailable, DjangoValidationError):
            return None
        if not found:
            return None
        return {
            'bank_code': found['bank_code'],
            'bank_name': found['display_name'],
            'gl_account': found['gl_account'],
            'account_number': found['account_number'],
            'branch': found['branch'],
            'bank_key': found['key'],
        }

    def validate(self, attrs):
        def current(field, default=None):
            if field in attrs:
                return attrs[field]
            if self.instance is not None:
                return getattr(self.instance, field, default)
            return default

        company = current('company')
        method = current('payment_method')

        if method == PaymentMethodEntry.Method.CASH:
            raise serializers.ValidationError({'payment_method': (
                'Cash does not use a house bank account. Set the cash G/L on '
                'the company mapping instead.')})

        if current('is_active', True) and company and method:
            clash = PaymentMethodMapping.objects.filter(
                company=company, payment_method=method, is_active=True)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError({'payment_method': (
                    f'{method} is already mapped for {company}. Edit that '
                    f'mapping instead of adding a second one.')})

        # A mapping that does not resolve would post to nothing, so it is
        # refused at entry rather than discovered at posting time.
        bank_key = current('bank_key')
        if bank_key:
            try:
                bank_master.find_bank(company, bank_key)
            except bank_master.BankMasterUnavailable as exc:
                raise serializers.ValidationError({'bank_key': str(exc)}) from exc
            except DjangoValidationError as exc:
                raise serializers.ValidationError(
                    {'bank_key': exc.messages}) from exc
        return attrs


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

        receipts = PaymentReceipt.objects.filter(
            id__in=receipt_ids, company=company)
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

        collected = sum((r.total_amount for r in receipts), Decimal('0'))
        deposit_amount = current('deposit_amount')
        if deposit_amount > collected:
            raise serializers.ValidationError({
                'deposit_amount':
                    f'Cannot deposit {deposit_amount}; only {collected} was collected.'})
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

        attrs['_receipts'] = list(receipts)
        attrs['_collected'] = collected
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

        # Same reasoning as the receipt edit above: record WHO changed it,
        # under an action that reads as an edit rather than a status move.
        log_status(instance, from_status=instance.status,
                   to_status=instance.status, user=user,
                   action=PaymentStatusHistory.Action.UPDATED,
                   reason='Deposit updated.')
        return instance
