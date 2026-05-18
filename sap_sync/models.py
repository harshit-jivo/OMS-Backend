from django.db import models
from django.utils import timezone

class Product(models.Model):
    """Product/Item synced from SAP"""
    
    CATEGORY_CHOICES = [
        ('OIL', 'Oil'),
        ('BEVERAGES', 'Beverages'),
        ('MART', 'Mart'),
    ]
        
    item_code = models.CharField(max_length=50)
    item_name = models.CharField(max_length=255, blank=True, null=True)
    category = models.CharField(max_length=20)
    sal_factor2 = models.DecimalField(max_digits=18, decimal_places=6, blank=True, null=True)
    tax_rate = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    is_deleted = models.CharField(max_length=1, default='N', blank=True, null=True)
    variety = models.CharField(max_length=100, blank=True, null=True)
    sal_pack_unit = models.CharField(max_length=50, blank=True, null=True)
    brand = models.CharField(max_length=100, blank=True, null=True)
        
    synced_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
        
    class Meta:
        db_table = 'sap_products'
        ordering = ['item_code']
        unique_together = ['item_code', 'category']

    def __str__(self):
        return f"{self.item_code} - {self.item_name}"

class Party(models.Model):
    card_code = models.CharField(max_length=50, db_index=True)  # Remove unique=True
    card_name = models.CharField(max_length=200)
    address = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    main_group = models.CharField(max_length=100, blank=True, null=True)
    chain = models.CharField(max_length=100, blank=True, null=True)
    country = models.CharField(max_length=50, blank=True, null=True)
    card_type = models.CharField(max_length=1, default='C')   
    category = models.CharField(max_length=20, blank=True, null=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sap_parties'
        ordering = ['card_code']
        unique_together = ['card_code', 'category']
        
    def __str__(self):
        return f"{self.card_code} - {self.card_name}"

class PartyAddress(models.Model):
    card_code = models.CharField(max_length=50, db_index=True)
    address_name = models.CharField(max_length=100)
    address_type = models.CharField(max_length=1)  # B or S
    gst_number = models.CharField(max_length=50, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    zip_code = models.CharField(max_length=20, blank=True, null=True)
    country = models.CharField(max_length=50, blank=True, null=True)
    full_address = models.TextField(blank=True, null=True)
    category = models.CharField(max_length=20, blank=True, null=True)
    synced_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'sap_party_addresses'
        ordering = ['card_code', 'address_name']
        unique_together = ['card_code', 'address_name', 'category']
    
    def __str__(self):
        return f"{self.card_code} - {self.address_name}"

class SyncLog(models.Model):
    """Log of all sync operations"""
    
    SYNC_TYPE_CHOICES = [
        ('PRODUCT', 'Products'),
        ('PARTY', 'Parties'),
        ('PARTY_ADDRESS', 'Party Addresses'),
        ('ALL', 'All'),
    ]
    
    STATUS_CHOICES = [
        ('STARTED', 'Started'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    ]
    
    sync_type = models.CharField(max_length=20, choices=SYNC_TYPE_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='STARTED')
    records_processed = models.IntegerField(default=0)
    records_created = models.IntegerField(default=0)
    records_updated = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    triggered_by = models.CharField(max_length=50, default='manual')  # manual, scheduled
    
    class Meta:
        db_table = 'sap_sync_logs'
        ordering = ['-started_at']
    
    def __str__(self):
        return f"{self.sync_type} - {self.status} - {self.started_at}"

class SyncSchedule(models.Model):
    """Schedule configuration for automated sync"""
    
    FREQUENCY_CHOICES = [
        ('HOURLY', 'Every Hour'),
        ('DAILY', 'Daily'),
        ('WEEKLY', 'Weekly'),
        ('CUSTOM', 'Custom Interval'),
    ]
    
    name = models.CharField(max_length=100)
    sync_type = models.CharField(max_length=20, choices=SyncLog.SYNC_TYPE_CHOICES, default='ALL')
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default='DAILY')
    custom_interval_minutes = models.IntegerField(default=60, help_text="Used when frequency is CUSTOM")
    hour = models.IntegerField(default=6, help_text="Hour of day for DAILY sync (0-23)")
    is_active = models.BooleanField(default=False)
    last_run = models.DateTimeField(blank=True, null=True)
    next_run = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'sap_sync_schedules'
    
    def __str__(self):
        return f"{self.name} - {self.frequency} - {'Active' if self.is_active else 'Inactive'}"

class Branch(models.Model):
    """SAP Business Place / Branch from OBPL table"""
    
    CATEGORY_CHOICES = [
        ('OIL', 'Oil'),
        ('BEVERAGES', 'Beverages'),
        ('MART', 'Mart'),
    ]
    
    bpl_id = models.IntegerField()
    bpl_name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'branches'
        unique_together = ['bpl_id', 'category']
        ordering = ['category', 'bpl_id']
        verbose_name = 'Branch'
        verbose_name_plural = 'Branches'

    def __str__(self):
        return f"{self.category} - {self.bpl_id} - {self.bpl_name}"
 
class SalesQuotationLog(models.Model):
    STATUS_CHOICES = [
        ('STARTED', 'Started'),
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
    ]
    
    order_id = models.CharField(max_length=100, blank=True, null=True)
    sap_doc_entry = models.IntegerField(blank=True, null=True)
    sap_doc_num = models.IntegerField(blank=True, null=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='STARTED')

    request_data = models.JSONField(blank=True, null=True)
    response_data = models.JSONField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'sales_quotation_logs'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.order_id} - {self.status}"