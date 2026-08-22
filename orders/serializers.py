from rest_framework import serializers
from .models import Parties,DispatchLocation,ProductDetails,OrderItem,Branches,OrdersLog,OrderItemScheme, Order,Notification,StaffProductPrice, OrderRateApproval, OrderItemApprovalMapping
from users.models import SchemeProduct, State
from sap_sync.models import PartyAddress as SapPartyAddress
from sap_sync.models import Product as SapProduct
from sap_sync.models import Party as SapParty
from decimal import Decimal

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
    # SchemeProduct has no `state` FK — migration 0010 replaced it with the plain
    # `state_code` column — so state_name is resolved from State by code.
    state_name = serializers.SerializerMethodField()
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

    def get_state_name(self, obj):
        code = (getattr(obj, 'state_code', '') or '').strip()
        if not code:
            return None
        # Cached on the serializer so a many=True render costs one query, not one
        # per row.
        if not hasattr(self, '_state_name_map'):
            self._state_name_map = {
                str(row['code']).strip().upper(): row['name']
                for row in State.objects.values('code', 'name')
                if row.get('code')
            }
        return self._state_name_map.get(code.upper(), code)

    class Meta:
        model = SchemeProduct
        fields = [
            'scheme_id',
            'scheme_name',
            'is_active',
            'state_name',
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
    warehouse_code = serializers.CharField(required=False, allow_blank=True, default='')
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
        raw = self._raw_remarks(obj)
        if self._is_billing_acceptance(obj):
            # Preserve a real comment the billing user typed; only fall back to
            # the generic "Accepted by billing" label when they approved with no
            # note (otherwise the typed remark is lost and never shown).
            auto_billing = {
                "",
                "approved",
                "accepted",
                "billing",
                "accepted by billing",
                "approved by billing",
                "sent to auditor",
            }
            if raw.strip().lower() in auto_billing:
                return "Accepted by billing"
            return raw
        if (
            self._action_name(obj).lower() == "completed"
            and self._performed_by_role(obj) == "auditor"
            and not raw
        ):
            return "Sales quotation created by auditor"
        return raw
   
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
    variety_type = serializers.SerializerMethodField()
    last_purchase_price = serializers.SerializerMethodField()


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


    def get_variety_type(self, obj):
        commodity_list = ["BLENDED","COTTON SEED","GIFT PACK", "GROUNDNUT","MUSTARD","PALMOLEIN","RICE BRAN","SESAME","SOYABEAN","SUNFLOWER"]
        premium_list = [
            "BLENDED",
            "CANOLA",
            "COCONUT",
            "DRY FRUITS/NUTS",
            "EXTRA VIRGIN",
            "GHEE",
            "GIFT PACK",
            "GROUNDNUT",
            "MUSTARD",
            "OLIVE",
            "SESAME",
            "SPICES"
        ]

        sub_group =  SapProduct.objects.filter(item_code=obj.item_code , category=obj.category).values_list('sub_group', flat=True).first()
        id = SapProduct.objects.filter(item_code=obj.item_code).values_list('id', flat=True).first()
        category = getattr(obj, 'category', None)

        if sub_group in commodity_list:
            variety_type = "COMMODITY"
        elif sub_group in premium_list:
            variety_type = "PREMIUM"
        else:
            variety_type = "OTHERS"

        print(f"{id} - {category} - {obj.item_name} - {sub_group} - {variety_type}")
        return variety_type
    

    def get_last_purchase_price(self, obj):
        card_code = getattr(obj.order, 'card_code', None)
        item_code = getattr(obj, 'item_code', None)

        current_order = getattr(obj, 'order', None)
        current_order_id = Order.objects.filter(id=current_order.id).values('id').first() if current_order else None
        # print(current_order_id)


        last_order_id = Order.objects.filter(
            card_code=card_code, 
            items__item_code=item_code,
            id__lt=current_order_id['id'] if current_order_id else None

        ).values('id').order_by('-created_at').first()
        
        if not last_order_id:
            return None
        else:
            last_purchase_price = OrderItem.objects.filter(order_id=last_order_id['id'], item_code=item_code).values_list('basic_price', flat=True).first()
            # print(last_order_id)0
            # print(f"Last purchase price for card_code: {card_code}, item_code: {item_code} is {last_purchase_price}")
            return last_purchase_price if last_purchase_price is not None else None
        

        



    class Meta:
        model = OrderItem
        fields = "__all__"

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
    # items = OrderItemSerializer(many=True, read_only=True)
    # items_count = serializers.IntegerField(source="items.count", read_only=True)
    # categories = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="status.name", read_only=True)
    created_by = serializers.IntegerField(source="created_by_id", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    # rate_approvals = OrderRateApprovalSerializer(many=True, read_only=True)

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

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "order_type",
            # "employee_id",
            "card_code",
            "card_name",
            # "bill_to_id",
            # "bill_to_address",
            # "ship_to_id",
            # "ship_to_address",
            # "dispatch_from_id",
            # "dispatch_from_name",
            # "company",
            # "po_number",
            "is_foc",
            # "remarks",
            "total_amount",
            "status",
            "status_name",
            "status_display",
            "created_by",
            "created_by_name",
            "created_at",   
            "delivery_date",  
            # "sap_doc_number",
            # "items",
            # "items_count",
            # "categories",
            # "rate_approvals",
        ]

class OrderDetailSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    items_count = serializers.IntegerField(source="items.count", read_only=True)
    status = serializers.CharField(source="status.code")
    status_display = serializers.CharField(source="status.name")
    party_state = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    rate_approvals = OrderRateApprovalSerializer(many=True, read_only=True)
    vareity_cost = serializers.SerializerMethodField()

    # commodity_total = serializers.SerializerMethodField()

    def get_party_state(self, obj):
        category = obj.items.first().category 
        party = SapParty.objects.filter(card_code=obj.card_code , category=category).first()
        if not party or not party.state:
            return None
       
        state = State.objects.filter(code=party.state).first()
        return state.name if state else party.state
    
    def get_vareity_cost(self, obj):
         
        commodity_list = ["BLENDED","COTTON SEED","GIFT PACK", "GROUNDNUT","MUSTARD","OLIVE","PALMOLEIN","RICE BRAN","SESAME","SOYABEAN","SUNFLOWER"]
        others_list = [
            "ATTA",
            "COFFEE",
            "DRINKS",
            "DRY FRUITS/NUTS",
            "FLAKES",
            "GHEE",
            "GIFT PACK",
            "HONEY",
            "RICE",
            "SEEDS",
            "SLICED OLIVE",
            "SNACKS",
            "SOYA CHUNK",
            "SPICES",
            "TEA",
            "VITAMINS"
        ]

        premium_list = [
            "BLENDED",
            "CANOLA",
            "COCONUT",
            "DRY FRUITS/NUTS",
            "EXTRA VIRGIN",
            "GHEE",
            "GIFT PACK",
            "GROUNDNUT",
            "MUSTARD",
            "OLIVE",
            "SESAME",
            "SPICES"
        ]

        commodity_price = Decimal(0)
        other_total = Decimal(0)
        premium_total = Decimal(0)

        for item in OrderItem.objects.filter(order=obj.id):
            product = SapProduct.objects.filter(item_code=item.item_code).first()
            product_group = product.sub_group if product else None 

            if product_group in commodity_list:
                category = "COMMODITY"
                commodity_price += item.total
            elif product_group in others_list:
                category = "OTHERS"
                other_total += item.total
            elif product_group in premium_list:
                category = "PREMIUM"
                premium_total += item.total
            else:
                category = "OTHERS"
                other_total += item.total


        return {
                    "commodity_price": commodity_price,
                    "other_total": other_total,
                    "premium_total": premium_total
                }


    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.username
        return None
    

    # def get_commodity_total(self, obj):
     
    class Meta:
        model = Order
        fields = [
            "id", "order_number", "card_code", "card_name",
            "bill_to_id", "bill_to_address", "ship_to_id", "ship_to_address",
            "dispatch_from_id", "dispatch_from_name", "company", "po_number",
            "warehouse_code", "is_foc",
            "remarks", "total_amount", "status", "status_display",
            "created_by", "created_by_name", "created_at", "delivery_date",
            "sap_created", "sap_doc_number", "quotation_cancelled",
            "approved_by", "approved_at", "rejected_by", "rejected_at",
            "rejection_reason", "reject_reason", "updated_at",
            "items", "items_count", "party_state",
            "rate_approvals", "vareity_cost"
        ]

class CreateSchemeSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchemeProduct
        fields = ["scheme_name", "item_code", "state_code"]
        extra_kwargs = {"state_code": {"required": False, "allow_blank": True, "allow_null": True}}


class SchemeWriteSerializer(serializers.ModelSerializer):
    """Create or update one scheme_product row.

    Rejects an exact duplicate of (scheme_name, state_code, item_code). Those rows
    are indistinguishable in every picker, and the table has no unique constraint
    to stop them — only PRIMARY KEY (scheme_id).
    """

    class Meta:
        model = SchemeProduct
        fields = ["scheme_id", "scheme_name", "item_code", "state_code", "is_active"]
        read_only_fields = ["scheme_id"]
        extra_kwargs = {
            "state_code": {"required": False, "allow_blank": True, "allow_null": True},
            "is_active": {"required": False},
        }

    def validate(self, attrs):
        instance = self.instance

        def resolved(field):
            if field in attrs:
                return attrs[field]
            return getattr(instance, field, None)

        duplicates = SchemeProduct.objects.filter(
            scheme_name=resolved("scheme_name"),
            item_code=resolved("item_code"),
            state_code=resolved("state_code"),
            is_active=True,
        )
        if instance is not None:
            duplicates = duplicates.exclude(scheme_id=instance.scheme_id)

        if duplicates.exists():
            raise serializers.ValidationError({
                "scheme_name": (
                    "An active scheme with this name, state and item already exists "
                    f"(scheme_id {duplicates.first().scheme_id})."
                )
            })
        return attrs


class NotificationSerializer(serializers.ModelSerializer):
    # Reads the local `order_id` COLUMN rather than traversing to `order.id`.
    # The traversal loaded the whole related Order per row, so serialising a
    # page without select_related('order') cost one extra query per
    # notification. Sourcing the column keeps the output byte-identical while
    # making the query plan independent of how the caller built the queryset.
    order_id = serializers.IntegerField(read_only=True)

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




# New Architecture Serializers 
class OrdersByItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ['id', 'order', 'item_code', 'item_name', 'category', 'brand', 'sub_group']

# ---------------------------------------------------------------------------
# Scheme engine v2 (see docs/scheme-architecture.md)
# ---------------------------------------------------------------------------

from .models import Scheme, SchemeBenefit, SchemeTrigger, SchemeAssignment


class SchemeBenefitSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchemeBenefit
        fields = ['id', 'free_item_code', 'free_uom', 'per_qty', 'free_qty', 'max_free_qty']

    def validate(self, attrs):
        per_qty = attrs.get('per_qty', getattr(self.instance, 'per_qty', 0)) or 0
        free_qty = attrs.get('free_qty', getattr(self.instance, 'free_qty', 0)) or 0
        # per_qty is a divisor; a ratio with nothing on the giveaway side would
        # silently produce zero free stock on every order.
        if per_qty > 0 and free_qty <= 0:
            raise serializers.ValidationError({
                'free_qty': 'A ratio benefit (per_qty > 0) must give away a positive free_qty.'
            })
        return attrs


class SchemeTriggerSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchemeTrigger
        fields = ['id', 'match_type', 'match_value', 'min_qty', 'min_uom', 'applies_to']

    def validate(self, attrs):
        match_type = attrs.get('match_type', getattr(self.instance, 'match_type', None))
        match_value = attrs.get('match_value', getattr(self.instance, 'match_value', '')) or ''
        if match_type != 'ALL' and not str(match_value).strip():
            raise serializers.ValidationError({
                'match_value': f'match_value is required when match_type is {match_type}.'
            })
        return attrs


class SchemeAssignmentSerializer(serializers.ModelSerializer):
    scheme_code = serializers.CharField(source='scheme.code', read_only=True)

    class Meta:
        model = SchemeAssignment
        fields = [
            'id', 'scheme', 'scheme_code', 'scope_type', 'scope_value', 'category',
            'is_exclusion', 'valid_from', 'valid_to', 'is_active', 'created_at',
        ]
        # `scheme` is a non-null FK, so DRF would demand it in the request body —
        # but the parent is always known from context: the URL for the assignments
        # endpoint, or the enclosing scheme for a nested write. Read-only keeps it
        # in the response without requiring callers to repeat it.
        read_only_fields = ['id', 'scheme', 'created_at']

    def validate(self, attrs):
        scope_type = attrs.get('scope_type', getattr(self.instance, 'scope_type', None))
        scope_value = attrs.get('scope_value', getattr(self.instance, 'scope_value', '')) or ''
        if scope_type == SchemeAssignment.SCOPE_ALL:
            attrs['scope_value'] = ''
        elif not str(scope_value).strip():
            raise serializers.ValidationError({
                'scope_value': f'scope_value is required when scope_type is {scope_type}.'
            })
        return attrs


class SchemeV2Serializer(serializers.ModelSerializer):
    """Read/write a scheme together with its benefits, triggers and assignments.

    Children are written wholesale: whatever list arrives replaces what is there.
    Partial updates that omit a child key leave that child set untouched.
    """

    benefits = SchemeBenefitSerializer(many=True, required=False)
    triggers = SchemeTriggerSerializer(many=True, required=False)
    assignments = SchemeAssignmentSerializer(many=True, required=False)

    class Meta:
        model = Scheme
        fields = [
            'id', 'code', 'name', 'description', 'category',
            'valid_from', 'valid_to', 'is_active', 'priority', 'stackable',
            'benefits', 'triggers', 'assignments',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        valid_from = attrs.get('valid_from', getattr(self.instance, 'valid_from', None))
        valid_to = attrs.get('valid_to', getattr(self.instance, 'valid_to', None))
        if valid_from and valid_to and valid_to < valid_from:
            raise serializers.ValidationError({'valid_to': 'valid_to cannot precede valid_from.'})
        return attrs

    def _write_children(self, scheme, benefits, triggers, assignments):
        if benefits is not None:
            # PROTECT on OrderItemScheme.scheme_v2 guards the parent; benefits use
            # SET_NULL, so replacing them blanks the link on historical rows but
            # leaves benefit_item_code — the snapshot that actually ships — intact.
            scheme.benefits.all().delete()
            SchemeBenefit.objects.bulk_create(
                [SchemeBenefit(scheme=scheme, **row) for row in benefits]
            )
        if triggers is not None:
            scheme.triggers.all().delete()
            SchemeTrigger.objects.bulk_create(
                [SchemeTrigger(scheme=scheme, **row) for row in triggers]
            )
        if assignments is not None:
            scheme.assignments.all().delete()
            user = getattr(self.context.get('request'), 'user', None)
            SchemeAssignment.objects.bulk_create([
                SchemeAssignment(
                    scheme=scheme,
                    created_by=user if getattr(user, 'is_authenticated', False) else None,
                    **{k: v for k, v in row.items() if k != 'scheme'},
                )
                for row in assignments
            ])

    def create(self, validated_data):
        benefits = validated_data.pop('benefits', [])
        triggers = validated_data.pop('triggers', [])
        assignments = validated_data.pop('assignments', [])
        user = getattr(self.context.get('request'), 'user', None)
        if getattr(user, 'is_authenticated', False):
            validated_data['created_by'] = user
        scheme = Scheme.objects.create(**validated_data)
        self._write_children(scheme, benefits, triggers, assignments)
        return scheme

    def update(self, instance, validated_data):
        benefits = validated_data.pop('benefits', None)
        triggers = validated_data.pop('triggers', None)
        assignments = validated_data.pop('assignments', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        self._write_children(instance, benefits, triggers, assignments)
        return instance
