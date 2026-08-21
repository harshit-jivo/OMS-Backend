from django.db import models
from users.models import User, PartyProductAssignment, UserPartyAssignment
from django.utils import timezone
from sap_sync.models import Product

class OrderStatus(models.Model):
    """
    Represents the status of an order.
    e.g., 'CREATED', 'APPROVED', 'COMPLETED'
    """
    code = models.CharField(max_length=50, unique=True,null=True)
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now)
    
    class Meta:
        db_table = 'order_statuses' 
        verbose_name = "Order Status"
        verbose_name_plural = "Order Statuses"

    def __str__(self):
        return self.name
    

class Parties(models.Model):
    card_code = models.CharField(max_length=50, unique=True)
    card_name = models.CharField(max_length=255)
    address = models.TextField(blank=True, null=True)
    state = models.CharField(max_length=50, blank=True, null=True)
    main_group = models.CharField(max_length=50, blank=True, null=True)
    
    class Meta:
        db_table = 'parties'
        verbose_name_plural = "Parties"

    def __str__(self):
        return f"{self.card_name} ({self.card_code})"

class DispatchLocation(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50)

    address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pincode = models.CharField(max_length=20, blank=True, null=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(null=True)

    class Meta:
        db_table = 'dispatch_locations'

    def __str__(self):
        return self.name

class PartyAddress(models.Model):
    card_code = models.CharField(max_length=50)
    full_address = models.TextField(null=True)
    gst_number = models.CharField(max_length=50, blank=True, null=True)
    address_type = models.CharField(max_length=10,null=True) # 'B' or 'S'
    address_name = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        db_table = 'party_addresses'

class ProductDetails(models.Model):
    item_code = models.CharField(max_length=50, unique=True)
    item_name = models.CharField(max_length=255)
    category = models.CharField(max_length=100, blank=True, null=True)
    brand = models.CharField(max_length=100, blank=True, null=True)
    variety = models.CharField(max_length=100, blank=True, null=True)
    sal_factor2 = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    sal_pack_unit = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        db_table = 'product_details'

class Branches(models.Model):
    bpl_id = models.CharField(max_length=50, blank=True, null=True)
    bpl_name = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        db_table = 'branches'
        managed = False
        verbose_name_plural = "Branches"
    
class Order(models.Model):
    order_number = models.CharField(max_length=50, unique=True)
    card_code = models.CharField(max_length=50)
    card_name = models.CharField(max_length=255)
    
    bill_to_id = models.IntegerField(default=0)
    bill_to_address = models.TextField(blank=True,null=True)
    ship_to_id = models.IntegerField(default=0)
    ship_to_address = models.TextField(blank=True,null=True)
    
    dispatch_from_id = models.IntegerField(default=0)
    dispatch_from_name = models.CharField(max_length=100, blank=True,null=True)
    
    company = models.CharField(max_length=100, blank=True,null=True)
    po_number = models.CharField(max_length=100, blank=True,null=True)
    # Chosen once for the whole order and stamped on every SAP line, free stock
    # included. Blank falls back to the per-category default in settings, which
    # is how every order placed before the picker existed still maps.
    warehouse_code = models.CharField(max_length=20, blank=True, default='')
    is_foc = models.BooleanField(default=False)
    remarks = models.TextField(blank=True,null=True)
    
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    status = models.ForeignKey(
        OrderStatus,
        on_delete=models.PROTECT,
        db_column='status_id',
        related_name='orders',
    )
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_orders')
    created_at = models.DateTimeField(auto_now_add=True)
    delivery_date = models.DateField(null=True, blank=True)
    
    ORDER_TYPES = (
    ("PARTY", "Party"),
    ("STAFF", "Staff"),
    ("DISTRIBUTOR", "Distributor"),
    )

    order_type = models.CharField(
    max_length=20,
    choices=ORDER_TYPES,
    default="PARTY"
    )

    employee_id = models.CharField(
    max_length=255,
    null=True,
    blank=True
    )

    sap_created = models.BooleanField(default=False)
    sap_doc_number = models.CharField(blank=True, max_length=100, null=True)
    # Set when a manager cancels the order's SAP Sales Quotation from the
    # View Orders page. The actual cancellation happens in SAP; this flag mirrors
    # it in OMS so the UI can show a "Quotation Cancelled" badge.
    quotation_cancelled = models.BooleanField(default=False)
    quotation_cancelled_at = models.DateTimeField(null=True, blank=True)
    quotation_cancelled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='cancelled_quotations',
    )
    # Approval/Rejection
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_orders')
    approved_at = models.DateTimeField(null=True, blank=True)
    
    rejected_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='rejected_orders')
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True,null=True)
    reject_reason = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'orders'

    def __str__(self):
        return self.order_number
class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255,null=True)
    category = models.CharField(max_length=100, blank=True,null=True)
    brand = models.CharField(max_length=100, blank=True,null=True)
    sub_group = models.CharField(max_length=100, blank=True,null=True)
    item_type = models.CharField(max_length=100, blank=True,null=True)

    qty = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pcs = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    boxes = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    ltrs = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    price_list_basic = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    basic_price = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    scheme = models.ForeignKey(
        'users.SchemeProduct',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='scheme_id',
        related_name='order_items'
    )
    qty_scheme = models.DecimalField(max_digits=10, decimal_places=2, default=0, null=True, blank=True)
    is_scheme_visible = models.BooleanField(default=False)
    # Set on the zero-priced line auto-added for the free half of a combo pack.
    # `combo_source_code` is the item_code of the combo that generated it; NULL/''
    # both mean "not part of a combo" (scheme_engine treats them identically). The
    # column is nullable (migration 0055_fix_combo_source_code_nullable drops the
    # stray NOT NULL that survived on some databases); the create path still writes
    # '' rather than NULL for tidiness.
    is_auto_free = models.BooleanField(default=False)
    combo_source_code = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = 'order_items'
    

class OrdersLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='logs')
    action = models.ForeignKey(OrderStatus, on_delete=models.SET_NULL, null=True)
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    remarks = models.TextField(blank=True,null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'orders_log'

class OrderFlowConfig(models.Model):
    flow_type = models.CharField(max_length=20, default='ASM', unique=True)
    rate_approval_enabled = models.BooleanField(default=True)
    billing_enabled = models.BooleanField(default=True)
    auditor_enabled = models.BooleanField(default=True)
    rate_conditions = models.JSONField(default=list, blank=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_order_flow_configs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'order_flow_config'
        verbose_name = 'Order Flow Config'
        verbose_name_plural = 'Order Flow Config'

    def __str__(self):
        return f'{self.flow_type} Order Flow Config'


class PartyOrderFlowConfig(models.Model):
    """Per-party, per-category, per-role order flow override. When a party has a
    row here for a given category + flow_type (ASM / BILLING), orders of that
    category and flow type for the party use these stage settings instead of the
    global OrderFlowConfig. An empty category matches any category."""
    card_code = models.CharField(max_length=50, db_index=True)
    category = models.CharField(max_length=50, blank=True, default='')
    flow_type = models.CharField(max_length=20, default='ASM')
    rate_approval_enabled = models.BooleanField(default=True)
    billing_enabled = models.BooleanField(default=True)
    auditor_enabled = models.BooleanField(default=True)
    rate_conditions = models.JSONField(default=list, blank=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_party_order_flow_configs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'party_order_flow_config'
        unique_together = ('card_code', 'category', 'flow_type')
        verbose_name = 'Party Order Flow Config'
        verbose_name_plural = 'Party Order Flow Config'

    def __str__(self):
        return f'Party Flow Config ({self.card_code} / {self.category or "ANY"} / {self.flow_type})'

def log_order_action(order, action_name, user=None, remarks=''):
    try:
        status_obj = OrderStatus.objects.get(name=action_name)
        OrdersLog.objects.create(
            order=order,
            action=status_obj,
            performed_by=user,
            remarks=remarks
        )
    except OrderStatus.DoesNotExist:
        pass
class Template(models.Model):
    temp_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_templates')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='template_instances')
    sub_group = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'order_template'
        unique_together = ('user', 'order') 
        
class OrderItemScheme(models.Model):
    order_item = models.ForeignKey(OrderItem, related_name='schemes', on_delete=models.CASCADE)
    scheme = models.ForeignKey(
        'users.SchemeProduct',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='scheme_id',
        related_name='order_item_schemes',
    )
    qty_scheme = models.DecimalField(max_digits=10, decimal_places=2, default=0, null=True, blank=True)

    # --- Scheme engine v2 ---------------------------------------------------
    # `scheme` above points at the legacy flat `scheme_product` row and stays
    # populated for historical orders. New orders resolved by scheme_engine fill
    # the fields below instead.
    scheme_v2 = models.ForeignKey(
        'orders.Scheme',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='order_item_schemes',
    )
    benefit = models.ForeignKey(
        'orders.SchemeBenefit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_item_schemes',
    )
    # Snapshot of the giveaway item as it stood when the order was placed. The
    # SAP push reads this instead of re-resolving from the scheme tables, so
    # editing a scheme can no longer change what an already-approved order ships.
    benefit_item_code = models.CharField(max_length=50, null=True, blank=True)
    # What the engine proposed, kept alongside `qty_scheme` (what was actually
    # granted) so a manual override stays visible.
    computed_qty = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_manual_override = models.BooleanField(default=False)
    # Why the scheme applied — 'STATE' / 'PB', 'PARTY' / 'CUSTA000123', ...
    scope_type = models.CharField(max_length=20, blank=True, default='')
    scope_value = models.CharField(max_length=100, blank=True, default='')

    class Meta:
        db_table = 'order_item_schemes'
class Categories(models.Model):
    category= models.CharField(max_length=255)

    class Meta:
        db_table = 'categories'


# ---------------------------------------------------------------------------
# Scheme engine v2
#
# The legacy `users.SchemeProduct` packs three concerns into one flat row: the
# offer identity (scheme_name), the giveaway item (item_code) and geography
# (state_code) — and a multi-item scheme is faked as several rows sharing a
# scheme_name, which sync_service then joins on by string. The four models below
# separate those concerns so a scheme can be targeted at one vendor or at every
# vendor in a state, and so its giveaway can hang off either half of a 1+1 combo.
#
# See docs/scheme-architecture.md.
# ---------------------------------------------------------------------------

UOM_CHOICES = (
    ('QTY', 'Qty'),
    ('PCS', 'Pieces'),
    ('BOX', 'Boxes'),
    ('LTR', 'Litres'),
)

# UOM code -> the OrderItem field holding that measure.
UOM_FIELDS = {
    'QTY': 'qty',
    'PCS': 'pcs',
    'BOX': 'boxes',
    'LTR': 'ltrs',
}


class Scheme(models.Model):
    """One offer. Carries no product, no geography and no vendor — those live in
    the benefit / trigger / assignment children."""

    CATEGORY_CHOICES = (
        ('OIL', 'Oil'),
        ('BEVERAGES', 'Beverages'),
        ('MART', 'Mart'),
    )

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')

    # The business line this offer belongs to. An OIL scheme never fires on a
    # MART or BEVERAGES line, however it was targeted — targeting says *who*
    # gets an offer, this says *what it is an offer on*. Blank = every category,
    # which is what every scheme created before this field existed means.
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, blank=True, default='', db_index=True,
    )

    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Higher priority wins when two schemes target the same line and the winner
    # is not stackable.
    priority = models.IntegerField(default=0)
    stackable = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_schemes',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'schemes'
        ordering = ['-priority', 'name']
        indexes = [
            models.Index(fields=['is_active', 'valid_from', 'valid_to'],
                         name='scheme_active_window_idx'),
        ]

    def __str__(self):
        return f'{self.code} — {self.name}'

    def is_live_on(self, on_date):
        if not self.is_active:
            return False
        if self.valid_from and on_date < self.valid_from:
            return False
        if self.valid_to and on_date > self.valid_to:
            return False
        return True


class SchemeBenefit(models.Model):
    """What a scheme gives away. Several rows = several giveaway items, replacing
    the old "N scheme_product rows sharing a scheme_name" fan-out."""

    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, related_name='benefits')

    # NULL means "the same item as the trigger line" — buy 10 boxes, get 1 free.
    free_item_code = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    free_uom = models.CharField(max_length=10, choices=UOM_CHOICES, default='PCS')

    # Ratio rule: buy `per_qty`, get `free_qty`. per_qty = 0 means a flat
    # giveaway of `free_qty` regardless of how much was ordered; free_qty = 0
    # with per_qty = 0 means the quantity is supplied by the user (this is what
    # every migrated legacy scheme becomes, preserving today's behaviour).
    per_qty = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    free_qty = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    max_free_qty = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = 'scheme_benefits'
        ordering = ['id']

    def __str__(self):
        return f'{self.scheme.code} -> {self.free_item_code or "(same item)"}'


class SchemeTrigger(models.Model):
    """What earns a scheme."""

    MATCH_ITEM = 'ITEM'
    MATCH_CHOICES = (
        (MATCH_ITEM, 'Item code'),
        ('SUB_GROUP', 'Sub group'),
        ('VARIETY', 'Variety'),
        ('BRAND', 'Brand'),
        ('CATEGORY', 'Category'),
        ('ALL', 'Any item'),
    )

    # Which half of a combo pack supplies the qualifying quantity.
    #   PAID_LINE — the ordered, priced quantity (ordinary single-FG scheme)
    #   FREE_LINE — the auto-added zero-priced companion of a 1+1
    #               (OrderItem.is_auto_free, combo_source_code = this item), so
    #               the giveaway is computed on top of the combo's free half
    #   BOTH      — the sum of the two
    APPLIES_TO_CHOICES = (
        ('PAID_LINE', 'Paid line'),
        ('FREE_LINE', 'Free (combo) line'),
        ('BOTH', 'Both'),
    )

    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, related_name='triggers')

    match_type = models.CharField(max_length=20, choices=MATCH_CHOICES, default=MATCH_ITEM)
    match_value = models.CharField(max_length=100, blank=True, default='')

    min_qty = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    min_uom = models.CharField(max_length=10, choices=UOM_CHOICES, default='QTY')

    applies_to = models.CharField(max_length=20, choices=APPLIES_TO_CHOICES, default='PAID_LINE')

    class Meta:
        db_table = 'scheme_triggers'
        ordering = ['id']
        indexes = [
            models.Index(fields=['match_type', 'match_value'], name='scheme_trigger_match_idx'),
        ]

    def __str__(self):
        return f'{self.scheme.code}: {self.match_type}={self.match_value or "*"}'


class SchemeAssignment(models.Model):
    """Who a scheme reaches.

    One STATE row covers every vendor in that state, including ones onboarded
    later; one PARTY row covers a single vendor. Specificity decides which wins
    (see SCOPE_SPECIFICITY), so a per-party row overrides the state default, and
    `is_exclusion` carves a vendor out of a state-wide scheme in one row.
    """

    SCOPE_PARTY = 'PARTY'
    SCOPE_MAIN_GROUP = 'MAIN_GROUP'
    SCOPE_STATE = 'STATE'
    SCOPE_CATEGORY = 'CATEGORY'
    SCOPE_ALL = 'ALL'

    SCOPE_CHOICES = (
        (SCOPE_PARTY, 'Party (card code)'),
        (SCOPE_MAIN_GROUP, 'Main group'),
        (SCOPE_STATE, 'State'),
        (SCOPE_CATEGORY, 'Category'),
        (SCOPE_ALL, 'All vendors'),
    )

    # Most specific scope wins. Ties are impossible — the unique constraint
    # allows at most one row per (scheme, scope_type, scope_value, category).
    SCOPE_SPECIFICITY = {
        SCOPE_PARTY: 100,
        SCOPE_MAIN_GROUP: 60,
        SCOPE_STATE: 50,
        SCOPE_CATEGORY: 20,
        SCOPE_ALL: 0,
    }

    scheme = models.ForeignKey(Scheme, on_delete=models.CASCADE, related_name='assignments')

    scope_type = models.CharField(max_length=20, choices=SCOPE_CHOICES, default=SCOPE_PARTY)
    # card_code / main_group / state code / category. Blank for ALL.
    scope_value = models.CharField(max_length=100, blank=True, default='')

    # Optional extra narrowing: the same item_code exists under OIL, BEVERAGES
    # and MART, so any scope can be limited to one of them. Blank = all.
    category = models.CharField(max_length=20, blank=True, default='')

    # A carve-out. Exclusions are absolute at every level, not specificity-ranked
    # — an explicit carve-out is always deliberate.
    is_exclusion = models.BooleanField(default=False)

    # Optional per-assignment narrowing of the scheme's own validity window.
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_scheme_assignments',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'scheme_assignments'
        unique_together = ('scheme', 'scope_type', 'scope_value', 'category')
        ordering = ['scheme_id', 'scope_type', 'scope_value']
        indexes = [
            models.Index(fields=['scope_type', 'scope_value', 'is_active'],
                         name='scheme_assign_scope_idx'),
        ]

    def __str__(self):
        target = self.scope_value or '*'
        prefix = 'EXCLUDE ' if self.is_exclusion else ''
        return f'{prefix}{self.scheme_id}: {self.scope_type}={target}'

    @property
    def specificity(self):
        return self.SCOPE_SPECIFICITY.get(self.scope_type, 0)

    def is_live_on(self, on_date):
        if not self.is_active:
            return False
        if self.valid_from and on_date < self.valid_from:
            return False
        if self.valid_to and on_date > self.valid_to:
            return False
        return True

class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='notifications')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at']
        indexes = [
            # Notification list is polled frequently and filters by recipient
            # ordered by recency (NotificationListView).
            models.Index(fields=['user', '-created_at'], name='notif_user_created_idx'),
            # Unread-count / mark-as-read queries filter by recipient + is_read.
            models.Index(fields=['user', 'is_read'], name='notif_user_isread_idx'),
        ]

    def __str__(self):
         return f"Notification for {self.user.username}: {self.message}"

class PushToken(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='push_tokens')
    token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=20, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'push_tokens'
        ordering = ['-updated_at']
        indexes = [
            # Push delivery resolves a user's active tokens on every send.
            models.Index(fields=['user', 'is_active'], name='pushtoken_user_active_idx'),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.platform}"


class WebPushSubscription(models.Model):
    """A browser Web Push subscription (Phase 3).

    Separate from :class:`PushToken` (Expo/mobile) so that web push work never
    touches the mobile token flow. One user can have many subscriptions (one
    per browser/device). ``endpoint`` is the unique push-service URL; ``p256dh``
    and ``auth`` are the encryption keys the browser hands us at subscribe time.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='web_push_subscriptions'
    )
    # Push endpoints can be long (FCM/Mozilla/WNS); TextField + unique index.
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'web_push_subscriptions'
        ordering = ['-updated_at']
        indexes = [
            # Delivery resolves a user's active subscriptions on every send.
            models.Index(fields=['user', 'is_active'], name='webpush_user_active_idx'),
        ]

    def __str__(self):
        return f"{self.user.username}: web ({self.endpoint[:32]}...)"


class StaffProductPrice(models.Model):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="staff_prices"
    )

    rate = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'staff_product_prices'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.product.item_name} - {self.rate}"
    
class RateApproverRule(models.Model):
    approver = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="rate_approver_rules"
    )

    category = models.CharField(max_length=100)

    variety = models.CharField(max_length=100, blank=True, null=True)

    sub_group = models.CharField(max_length=100, blank=True, null=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rate_approver_rules"
        unique_together = ("category", "sub_group")

    def __str__(self):
        return f"{self.category} / {self.sub_group} -> {self.approver}"


class OrderRateApproval(models.Model):

    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="rate_approvals"
    )

    approver = models.ForeignKey(
    User,
    on_delete=models.CASCADE,
    related_name="order_rate_approvals"
)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="PENDING"
    )

    remarks = models.TextField(
        blank=True,
        null=True
    )

    approved_at = models.DateTimeField(
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        db_table = "order_rate_approvals"
        unique_together = ("order", "approver")

class OrderItemApprovalMapping(models.Model):

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE
    )

    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.CASCADE
    )

    approver = models.ForeignKey(
    User,
    on_delete=models.CASCADE,
    related_name="item_approval_mappings"
)

    class Meta:
        db_table = "order_item_approval_mapping"
        unique_together = (
        "order_item",
        "approver"
    )
