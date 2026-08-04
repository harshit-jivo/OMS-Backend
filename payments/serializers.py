"""Serializers with REAL nested writes.

No nested-write serializer exists anywhere in this project — order+items uses
`ListField(DictField())` (orders/serializers.py:99), which applies zero
validation to any child field, and the rows are written by a manual view helper
with no transaction. Here the children are typed serializers and `create()` is
atomic.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from attachments.serializers import AttachmentSerializer
from core.models import next_document_number

from .models import (
    BankAccount,
    BankDeposit,
    BankDepositLine,
    CashDenomination,
    CollectionPerson,
    PaymentAllocation,
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
    SapCompanyMap,
    SapPostingHistory,
)


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

class SapCompanyMapSerializer(serializers.ModelSerializer):
    """Company -> SAP database mapping.

    `company_db` and `hana_schema` were previously omitted, so the admin UI
    rendered blank columns for the two fields that actually matter — and there
    was no way to see or fix a mapping outside Django admin.
    """

    class Meta:
        model = SapCompanyMap
        fields = ['id', 'company', 'display_name', 'company_db', 'hana_schema',
                  'default_bpl_id', 'is_active', 'sort_order']

    def validate_company(self, value):
        """`company` is unique — report the clash as a field error, not a 500."""
        qs = SapCompanyMap.objects.filter(company=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                f'A mapping for {value} already exists. Edit that one instead.')
        return value


class CollectionPersonSerializer(serializers.ModelSerializer):
    # `code` is globally unique but is an internal identifier the admin has no
    # reason to invent — generated from the name when omitted.
    code = serializers.CharField(required=False, allow_blank=True, max_length=30)

    class Meta:
        model = CollectionPerson
        fields = ['id', 'name', 'code', 'company', 'phone', 'sap_slp_code',
                  'is_active']

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


class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        # sap_gl_account is the SAP posting target — a deposit cannot post
        # without it, and (company, sap_gl_account) is UNIQUE. It was missing
        # from this list, so the admin UI could neither set nor see it and two
        # accounts in one company collided on an empty value.
        fields = ['id', 'name', 'company', 'account_type', 'masked_number',
                  'sap_gl_account',
                  'ifsc', 'branch_name', 'is_active']
        # DRF's auto-generated UniqueTogetherValidator runs BEFORE validate()
        # and reports "must make a unique set", which names neither the field
        # at fault nor the account already using it. Cleared so validate()
        # below owns the message; the DB constraint still backs it up.
        validators = []

    def validate(self, attrs):
        """Report the (company, sap_gl_account) clash as a field error, not a 500."""
        company = attrs.get('company', getattr(self.instance, 'company', None))
        gl = attrs.get('sap_gl_account',
                       getattr(self.instance, 'sap_gl_account', None))
        if company is None or gl is None:
            return attrs
        clash = BankAccount.objects.filter(company=company, sap_gl_account=gl)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({
                'sap_gl_account': (
                    f'GL account "{gl}" is already used by another '
                    f'{company} account.'
                )
            })
        return attrs


# ---------------------------------------------------------------------------
# Receipt children
# ---------------------------------------------------------------------------

class CashDenominationSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True)

    class Meta:
        model = CashDenomination
        fields = ['id', 'denomination', 'quantity', 'line_total']


class PaymentMethodEntrySerializer(serializers.ModelSerializer):
    denominations = CashDenominationSerializer(many=True, required=False)

    class Meta:
        model = PaymentMethodEntry
        fields = ['id', 'method', 'amount', 'upi_reference', 'cheque_number',
                  'bank_name', 'cheque_date', 'sap_check_key', 'denominations']
        read_only_fields = ['sap_check_key']

    def validate(self, attrs):
        method = attrs.get('method')
        if method == PaymentMethodEntry.Method.CHEQUE:
            if not attrs.get('cheque_number'):
                raise serializers.ValidationError(
                    {'cheque_number': 'Required for a cheque payment.'})
            if not attrs.get('cheque_date'):
                raise serializers.ValidationError(
                    {'cheque_date': 'Required for a cheque payment.'})

        # If a cash breakdown is supplied it must equal the cash amount. This
        # mirrors the rule already enforced in the mobile UI.
        rows = attrs.get('denominations') or []
        if rows:
            if method != PaymentMethodEntry.Method.CASH:
                raise serializers.ValidationError(
                    {'denominations': 'Only a cash entry may carry denominations.'})
            counted = sum(
                (Decimal(r['denomination']) * r['quantity'] for r in rows),
                Decimal('0'))
            if counted != attrs.get('amount'):
                raise serializers.ValidationError({
                    'denominations':
                        f'Denominations total {counted} but the cash amount is '
                        f'{attrs.get("amount")}.'})
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


class SapPostingHistorySerializer(serializers.ModelSerializer):
    """Read-only. Every field is read_only so the API cannot rewrite history."""

    action_display = serializers.CharField(
        source='get_action_display', read_only=True)
    status_display = serializers.CharField(
        source='get_status_display', read_only=True)

    class Meta:
        model = SapPostingHistory
        fields = ['id', 'attempt_number', 'action', 'action_display',
                  'status', 'status_display', 'sap_doc_entry', 'sap_doc_num',
                  'sap_response', 'created_by_username', 'created_at']
        read_only_fields = fields


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
                  'sap_doc_entry', 'sap_doc_num', 'sap_posted_at',
                  'sap_response',
                  'methods', 'allocations', 'attachments', 'approval',
                  'created_by', 'created_by_name', 'created_by_username',
                  'created_at', 'updated_at']
        read_only_fields = fields

    def get_approval(self, obj):
        request = obj.approvals.order_by('-created_at').first()
        if not request:
            return None
        return {
            'id': request.id,
            'status': request.status,
            'current_level': request.current_level,
            'total_levels': request.total_levels,
            'level_label': request.level_label,
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
                  'methods', 'allocations']

    def validate(self, attrs):
        if attrs.get('received_from_type') == PaymentReceipt.ReceivedFromType.PERSON \
                and not attrs.get('received_from_person'):
            raise serializers.ValidationError({
                'received_from_person':
                    'Required when the payment is received from a company person.'})

        methods = attrs.get('methods') or []
        if not methods:
            raise serializers.ValidationError(
                {'methods': 'Add at least one payment method.'})

        total = sum((m['amount'] for m in methods), Decimal('0'))
        allocations = attrs.get('allocations') or []
        allocated = sum((a['amount_applied'] for a in allocations), Decimal('0'))

        if allocated > total:
            raise serializers.ValidationError({
                'allocations':
                    f'Allocated {allocated} exceeds the payment total {total}.'})
        if not attrs.get('is_advance') and not allocations:
            raise serializers.ValidationError({
                'allocations':
                    'Select at least one invoice, or mark this as an advance.'})
        return attrs

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


# ---------------------------------------------------------------------------
# Deposit
# ---------------------------------------------------------------------------

class BankDepositLineSerializer(serializers.ModelSerializer):
    receipt_no = serializers.CharField(source='receipt.receipt_no', read_only=True)
    card_name = serializers.CharField(source='receipt.card_name', read_only=True)

    class Meta:
        model = BankDepositLine
        fields = ['id', 'receipt', 'receipt_no', 'card_name', 'amount']


class BankDepositSerializer(serializers.ModelSerializer):
    lines = BankDepositLineSerializer(many=True, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    bank_account_name = serializers.CharField(
        source='bank_account.name', read_only=True)
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
                  'bank_account', 'bank_account_name', 'deposit_type',
                  'collected_amount', 'deposit_amount', 'shortfall',
                  'shortfall_reason', 'bank_charge', 'currency',
                  'slip_number', 'remarks', 'status', 'status_display',
                  'sap_doc_entry', 'sap_doc_num', 'sap_posted_at',
                  'sap_response',
                  'lines', 'attachments', 'approval',
                  'created_by', 'created_by_name', 'created_by_username',
                  'created_at', 'updated_at']
        read_only_fields = fields

    def get_approval(self, obj):
        request = obj.approvals.order_by('-created_at').first()
        if not request:
            return None
        return {
            'id': request.id,
            'status': request.status,
            'current_level': request.current_level,
            'total_levels': request.total_levels,
            'level_label': request.level_label,
        }


class BankDepositCreateSerializer(serializers.ModelSerializer):
    """Create a deposit from a set of posted receipts."""

    receipt_ids = serializers.ListField(
        child=serializers.IntegerField(), write_only=True, allow_empty=False)

    class Meta:
        model = BankDeposit
        fields = ['id', 'company', 'deposit_date', 'deposited_by',
                  'bank_account', 'deposit_type', 'deposit_amount',
                  'shortfall_reason', 'bank_charge', 'currency',
                  'slip_number', 'remarks', 'receipt_ids']

    def validate(self, attrs):
        receipts = PaymentReceipt.objects.filter(
            id__in=attrs['receipt_ids'], company=attrs['company'])
        if receipts.count() != len(set(attrs['receipt_ids'])):
            raise serializers.ValidationError({
                'receipt_ids':
                    'One or more payments do not exist in the selected company.'})

        # Banked already? The answer lives in BankDepositLine, which is the only
        # written link and holds the unique constraint on `receipt` that makes
        # double-banking impossible at the database level.
        already = receipts.filter(deposit_lines__isnull=False).values_list(
            'receipt_no', flat=True)
        if already:
            raise serializers.ValidationError({
                'receipt_ids': f'Already deposited: {", ".join(already)}.'})

        collected = sum((r.total_amount for r in receipts), Decimal('0'))
        deposit_amount = attrs['deposit_amount']
        if deposit_amount > collected:
            raise serializers.ValidationError({
                'deposit_amount':
                    f'Cannot deposit {deposit_amount}; only {collected} was collected.'})
        if deposit_amount < collected and not (attrs.get('shortfall_reason') or '').strip():
            raise serializers.ValidationError({
                'shortfall_reason':
                    'A reason is required when depositing less than collected.'})

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
