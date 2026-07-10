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

    class Meta:
        db_table = 'order_item_schemes'
class Categories(models.Model):
    category= models.CharField(max_length=255)

    class Meta:
        db_table = 'categories'

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
