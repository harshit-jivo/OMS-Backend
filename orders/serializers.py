from rest_framework import serializers
from .models import Parties,DispatchLocation,ProductDetails,OrderItem,Branches,OrdersLog,OrderItemScheme, Order,Notification,StaffProductPrice, OrderRateApproval, OrderItemApprovalMapping
from users.models import SchemeProduct, State, User
from sap_sync.models import PartyAddress as SapPartyAddress
from sap_sync.models import Product as SapProduct
from sap_sync.models import Party as SapParty


def get_scheme_item_code_raw(scheme_id):
    if not scheme_id:
        return None

    return (
        SchemeProduct.objects
        .filter(scheme_id=scheme_id)
        .values_list('item_code', flat=True)
        .first()
    )

class SchemeProductSerializer(serializers.ModelSerializer):
    state_name = serializers.CharField(source='state.name', read_only=True)
    product_id = serializers.SerializerMethodField()
    item_name = serializers.SerializerMethodField()
    sal_factor2 = serializers.SerializerMethodField()
    sal_pack_unit = serializers.SerializerMethodField()

    def _get_product(self, obj):
        item_code = getattr(obj, 'item_code', None)
        if not item_code:
            return None
        return SapProduct.objects.filter(item_code=item_code).order_by('id').first()

    def get_product_id(self, obj):
        product = self._get_product(obj)
        return product.id if product else None

    def get_item_name(self, obj):
        product = self._get_product(obj)
        return product.item_name if product else None

    def get_sal_factor2(self, obj):
        product = self._get_product(obj)
        return product.sal_factor2 if product else None

    def get_sal_pack_unit(self, obj):
        product = self._get_product(obj)
        return product.sal_pack_unit if product else None
   
    class Meta:
        model = SchemeProduct
        fields = [
            'scheme_id',
            'scheme_name',
            'is_active',
            'state',
            'state_code',
            'product_id',
            'item_code',
            'item_name',
            'sal_factor2',
            'sal_pack_unit',
        ]
   

class PartiesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Parties
        fields = ['card_code','card_name']

class DispatchLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = DispatchLocation
        fields = ['id','name', 'code']

class PartyAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = SapPartyAddress
        fields = ['id','full_address', 'gst_number','address_type','address_name','category']
       
class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductDetails
        fields = ['id', 'item_code', 'item_name', 'category', 'brand', 'variety', 'sal_factor2', 'tax_rate', 'sal_pack_unit']

class CreateOrderSerializer(serializers.Serializer):
    card_code = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    card_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    bill_to_id = serializers.IntegerField(required=False, default=0)
    bill_to_address = serializers.CharField(required=False, allow_blank=True, default='')
    ship_to_id = serializers.IntegerField(required=False, default=0)
    ship_to_address = serializers.CharField(required=False, allow_blank=True, default='')
    dispatch_from_id = serializers.IntegerField(required=False, default=0)
    dispatch_from_name = serializers.CharField(required=False, allow_blank=True, default='')
    company = serializers.CharField(required=False, allow_blank=True, default='')
    po_number = serializers.CharField(required=False, allow_blank=True, default='')
    is_foc = serializers.BooleanField(required=False, default=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    items = serializers.ListField(child=serializers.DictField())
    price_list_basic = serializers.DecimalField(max_digits=12, decimal_places=4, default=0)
    delivery_date = serializers.DateField(required=False, allow_null=True)
    order_type = serializers.CharField(required=False, allow_blank=True, default='PARTY')
    employee_id = serializers.CharField(required=False, allow_blank=True, default='')
   

class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branches
        fields = ['bpl_id', 'bpl_name', 'category']

class OrderStatusUpdateSerializer(serializers.Serializer):
    status = serializers.IntegerField()
    reason = serializers.CharField(required=False, allow_blank=True)

class OrdersLogSerializer(serializers.ModelSerializer):
    status_id = serializers.IntegerField(source="action.id", read_only=True)
    status_name = serializers.SerializerMethodField()
    remarks = serializers.SerializerMethodField()
    performed_by_name = serializers.CharField(
        source="performed_by.username", read_only=True
    )

    def _performed_by_role(self, obj):
        role = getattr(getattr(obj, "performed_by", None), "role", None)
        return (getattr(role, "name", "") or "").strip().lower()

    def _action_name(self, obj):
        return (getattr(getattr(obj, "action", None), "name", "") or "").strip()

    def _raw_remarks(self, obj):
        return (getattr(obj, "remarks", "") or "").strip()

    def _is_billing_acceptance(self, obj):
        action_name = self._action_name(obj).lower()
        remarks = self._raw_remarks(obj).lower()
        performer_role = self._performed_by_role(obj)

        if "reject" in action_name or "reject" in remarks:
            return False

        return (
            performer_role == "billing"
            and (
                action_name in ["billing", "approved", "accepted"]
                or "billing" in action_name
                or remarks in ["approved", "accepted", "accepted by billing", "approved by billing", "sent to auditor"]
            )
        )

    def get_status_name(self, obj):
        if self._is_billing_acceptance(obj):
            return "Accepted by Billing"
        return self._action_name(obj)

    def get_remarks(self, obj):
        if self._is_billing_acceptance(obj):
            return "Accepted by billing"
        if (
            self._action_name(obj).lower() == "completed"
            and self._performed_by_role(obj) == "auditor"
            and not self._raw_remarks(obj)
        ):
            return "Sales quotation created by auditor"
        return self._raw_remarks(obj)
   
    class Meta:
        model = OrdersLog
        fields = [
            "id",
            "status_id",
            "status_name",
            "remarks",
            "performed_by_name",
            "created_at",
        ]

class OrderItemSchemeSerializer(serializers.ModelSerializer):
    scheme_id = serializers.IntegerField(read_only=True, allow_null=True)
    scheme_name = serializers.SerializerMethodField()
    scheme_item_code = serializers.SerializerMethodField()
    scheme_qty = serializers.DecimalField(source='qty_scheme', max_digits=10, decimal_places=2, read_only=True)

    def get_scheme_name(self, obj):
        raw_scheme_id = getattr(obj, 'scheme_id', None)
        if not raw_scheme_id:
            return None
        return (
            SchemeProduct.objects
            .filter(scheme_id=raw_scheme_id)
            .values_list('scheme_name', flat=True)
            .first()
        )

    def get_scheme_item_code(self, obj):
        raw_scheme_id = getattr(obj, 'scheme_id', None)
        if not raw_scheme_id:
            return None
        return get_scheme_item_code_raw(raw_scheme_id)

    class Meta:
        model = OrderItemScheme
        fields = ['id', 'scheme_id', 'scheme_name', 'scheme_item_code', 'scheme_qty', 'qty_scheme']

class OrderItemSerializer(serializers.ModelSerializer):
    scheme_id = serializers.IntegerField(read_only=True, allow_null=True)
    scheme_name = serializers.SerializerMethodField()
    scheme_item_code = serializers.SerializerMethodField()
    is_scheme_visible = serializers.SerializerMethodField()
    schemes = OrderItemSchemeSerializer(many=True, read_only=True)
    approval_approvers = serializers.SerializerMethodField()
    # Backward-compat: the column was renamed variety -> sub_group. Keep exposing
    # `variety` (read-only) so existing clients reading item.variety keep working.
    variety = serializers.CharField(source='sub_group', read_only=True)

    def get_scheme_name(self, obj):
        raw_scheme_id = getattr(obj, 'scheme_id', None)
        if not raw_scheme_id:
            return None
        return (
            SchemeProduct.objects
            .filter(scheme_id=raw_scheme_id)
            .values_list('scheme_name', flat=True)
            .first()
        )

    def get_scheme_item_code(self, obj):
        raw_scheme_id = getattr(obj, 'scheme_id', None)
        if not raw_scheme_id:
            return None
        return get_scheme_item_code_raw(raw_scheme_id)

    def get_is_scheme_visible(self, obj):
        raw_scheme_id = getattr(obj, 'scheme_id', None)
        scheme_qty = getattr(obj, 'qty_scheme', 0) or 0
        has_multiple_schemes = obj.schemes.exists() if getattr(obj, 'pk', None) else False
        return bool(
            getattr(obj, 'is_scheme_visible', False)
            or (raw_scheme_id and scheme_qty > 0)
            or has_multiple_schemes
        )

    def get_approval_approvers(self, obj):
        mappings = OrderItemApprovalMapping.objects.filter(order_item=obj).select_related('approver')
        return [
            {
                'id': mapping.approver_id,
                'name': getattr(mapping.approver, 'name', None) or getattr(mapping.approver, 'username', ''),
            }
            for mapping in mappings
            if mapping.approver_id
        ]

    class Meta:
        model = OrderItem
        fields = "__all__"

# Statuses where the order is sitting on someone's desk, grouped by who holds it.
PENDING_RATE_APPROVAL_CODES = {"RATE_APPROVAL", "NEED_APPROVAL"}
PENDING_AUDITOR_CODES = {"AUDITOR_APPROVAL"}
PENDING_BILLING_CODES = {"BILLING", "BILLING_PENDING"}


def _user_display_name(user):
    if not user:
        return ""
    return (getattr(user, "name", "") or getattr(user, "username", "") or "").strip()


def _active_role_user_names(role_name, cache=None):
    if cache is not None and role_name in cache:
        return cache[role_name]
    names = [
        name
        for name in (
            _user_display_name(u)
            for u in User.objects.filter(role__name__iexact=role_name, is_active=True)
        )
        if name
    ]
    if cache is not None:
        cache[role_name] = names
    return names


def compute_pending_with(order, cache=None):
    """Names of whoever the order is currently waiting on, based on its status.

    - Rate/need approval -> the rate approver(s) still PENDING on this order.
    - Auditor approval    -> the active auditor(s).
    - Billing             -> the active billing user(s).
    Terminal/other statuses return an empty list (no one is pending).
    """
    status_code = (getattr(getattr(order, "status", None), "code", "") or "").upper()
    if status_code in PENDING_RATE_APPROVAL_CODES:
        return [
            name
            for name in (
                _user_display_name(a.approver)
                for a in order.rate_approvals.all()
                if a.status == "PENDING"
            )
            if name
        ]
    if status_code in PENDING_AUDITOR_CODES:
        # Auditors are a flat pool (no per-order routing) — list the active ones.
        return _active_role_user_names("auditor", cache)
    if status_code in PENDING_BILLING_CODES:
        # Billing is routed by category/main-group, so only the billing user(s)
        # who actually need to act on this order are returned. Lazy import keeps
        # serializers <-> views from importing each other at module load.
        from .views import _billing_users_for_order

        return [
            name
            for name in (_user_display_name(u) for u in _billing_users_for_order(order))
            if name
        ]
    return []


class OrderRateApprovalSerializer(serializers.ModelSerializer):
    approver_name = serializers.SerializerMethodField()

    def get_approver_name(self, obj):
        return getattr(obj.approver, 'name', None) or getattr(obj.approver, 'username', '')

    class Meta:
        model = OrderRateApproval
        fields = [
            "id",
            "approver",
            "approver_name",
            "status",
            "remarks",
            "approved_at",
            "created_at",
        ]

class OrderListByUserIdSerializer(serializers.ModelSerializer):
    status_name = serializers.CharField(source="status.name")
    items = OrderItemSerializer(many=True, read_only=True)
    items_count = serializers.IntegerField(source="items.count", read_only=True)
    categories = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="status.name", read_only=True)
    created_by = serializers.IntegerField(source="created_by_id", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    rate_approvals = OrderRateApprovalSerializer(many=True, read_only=True)
    pending_with = serializers.SerializerMethodField()

    def get_categories(self, obj):
        return list(
            obj.items.exclude(category__isnull=True)
            .exclude(category__exact="")
            .values_list("category", flat=True)
            .distinct()
        )

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.username
        return None

    def get_pending_with(self, obj):
        cache = getattr(self, "_pending_with_role_cache", None)
        if cache is None:
            cache = {}
            self._pending_with_role_cache = cache
        return compute_pending_with(obj, cache)

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "order_type",
            "employee_id",
            "card_code",
            "card_name",
            "bill_to_id",
            "bill_to_address",
            "ship_to_id",
            "ship_to_address",
            "dispatch_from_id",
            "dispatch_from_name",
            "company",
            "po_number",
            "is_foc",
            "remarks",
            "total_amount",
            "status",
            "status_name",
            "status_display",
            "created_by",
            "created_by_name",
            "created_at",
            "delivery_date",
            "sap_doc_number",
            "items",
            "items_count",
            "categories",
            "rate_approvals",
            "pending_with",
        ]

class OrderDetailSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    items_count = serializers.IntegerField(source="items.count", read_only=True)
    status = serializers.CharField(source="status.code")
    status_display = serializers.CharField(source="status.name")
    party_state = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    rate_approvals = OrderRateApprovalSerializer(many=True, read_only=True)
    pending_with = serializers.SerializerMethodField()

    def get_party_state(self, obj):
        party = SapParty.objects.filter(card_code=obj.card_code).first()
        if not party or not party.state:
            return None
        state = State.objects.filter(code=party.state).first()
        return state.name if state else party.state

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.username
        return None

    def get_pending_with(self, obj):
        return compute_pending_with(obj)

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "card_code", "card_name",
            "bill_to_id", "bill_to_address", "ship_to_id", "ship_to_address",
            "dispatch_from_id", "dispatch_from_name", "company", "po_number","is_foc",
            "remarks", "total_amount", "status", "status_display",
            "created_by", "created_by_name", "created_at", "delivery_date",
            "sap_created", "sap_doc_number", "quotation_cancelled",
            "approved_by", "approved_at", "rejected_by", "rejected_at",
            "rejection_reason", "reject_reason", "updated_at",
            "items", "items_count", "party_state",
            "rate_approvals",
            "pending_with",
        ]

class CreateSchemeSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchemeProduct
        fields = ["scheme_name", "item_code", "state_code"]
        extra_kwargs = {"state_code": {"required": False, "allow_blank": True, "allow_null": True}}

class NotificationSerializer(serializers.ModelSerializer):
    order_id = serializers.IntegerField(source='order.id', read_only=True)
    
    class Meta:
        model = Notification
        fields = ['id', 'message', 'is_read', 'created_at', 'order_id']


class StaffProductSerializer(serializers.ModelSerializer):
    staff_rate = serializers.SerializerMethodField()

    class Meta:
        model = SapProduct
        fields = [
            "id",
            "item_code",
            "item_name",
            "category",
            "tax_rate",
            "sal_factor2",
            "sal_pack_unit",
            "staff_rate"
        ]

    def get_staff_rate(self, obj):
        staff_price = StaffProductPrice.objects.filter(
            product=obj
        ).first()

        return staff_price.rate if staff_price else 0
