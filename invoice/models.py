from django.db import models
from django.conf import settings

    
    
class InvoiceLog(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('ERROR', 'Error'),  # Added ERROR to choices
    ]
    
    so_number = models.CharField(max_length=100)
    party_name = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    ref_id = models.CharField(max_length=25 , null=True , blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    invoice_payload = models.JSONField()

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        blank=True, 
        null=True, 
        related_name='approved_invoices' # Added related_name to avoid clashes
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        blank=True, 
        null=True, 
        related_name='rejected_invoices' # Added related_name to avoid clashes
    )
    rejection_reason = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True) # Added the missing field

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='invoice_logs'
    )
    class Meta:
        db_table = 'invoice_log'
        
    def __str__(self):
        return f"InvoiceLog {self.so_number} - {self.status}"
    
    def save(self, *args, **kwargs):
        if self.status == 'REJECTED' and not self.rejection_reason:
            self.error_message = 'Invoice rejected without specific reason.'
        
        if self.status == 'ERROR' and not self.error_message:
            self.error_message = 'An error occurred during invoice processing.'
            
        super().save(*args, **kwargs)

        
class InvoiceRefLogs(models.Model):
    ref_id = models.CharField(max_length=25)
    card_name =  models.CharField(max_length=255)
    doc_date = models.DateField()
    so_number =  models.CharField(max_length=255)
    
    status = models.CharField(max_length = 255)
    error_message = models.TextField(blank = True , null = True)
        
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    posted_at = models.DateTimeField(auto_now_add = True)
    
    class Meta:
        db_table = 'invoice_ref_logs'


