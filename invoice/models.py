from django.db import models
from django.conf import settings

    
class InvocieHistory(models.Model):
    
    invoice_log = models.ForeignKey('InvoiceLog', on_delete=models.CASCADE, related_name='history' , blank=True, null=True)
    so_number = models.CharField(max_length=100)
    party_name = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20)
    invoice_payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='invoice_history')
    
    class Meta:
        db_table = 'invoice_history'
        
    def __str__(self):
        return f"InvoiceHistory {self.so_number} - {self.status}"
    
    
    
    
class InvoiceLog(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('ERROR', 'Error'),
        ('POSTED_TO_SAP' , 'Posted to SAP')
    ]
    
    so_number = models.CharField(max_length=100)
    party_name = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
        
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    rejection_reason = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    invoice_payload = models.JSONField()

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='invoice_logs')
    
    class Meta:
        db_table = 'invoice_log'
        
    def __str__(self):
        return f"InvoiceLog {self.so_number} - {self.status}"
    
    def save(self, *args, **kwargs):
        if self.status == 'REJECTED' and not self.rejection_reason:
            self.error_message = 'Invoice rejected without specific reason.'
        
        if self.status == 'ERROR' and not self.error_message:
            self.error_message = 'An error occurred during invoice processing.'
            
        is_new = self.pk is None # Optional tracking variable if you only want to know if it's new
        super().save(*args, **kwargs) 
        
        InvocieHistory.objects.create(
            invoice_log=self,
            so_number=self.so_number,
            party_name=self.party_name,
            total_amount=self.total_amount,
            status=self.status,
            invoice_payload=self.invoice_payload,
            created_by=self.created_by
        )


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