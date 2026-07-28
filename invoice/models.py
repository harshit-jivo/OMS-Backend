from django.db import models
from django.conf import settings

    
class InvocieHistory(models.Model):
    
    invoice_log = models.ForeignKey('InvoiceLog', on_delete=models.CASCADE, related_name='history' , blank=True, null=True)
    so_number = models.CharField(max_length=100)
    party_name = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20)
    # Archived alongside the status so a reviewer's reason survives even after the
    # live log's rejection_reason / error_message is cleared or overwritten.
    rejection_reason = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    invoice_payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=125 , null=True , blank=True)
    
    class Meta:
        db_table = 'invoice_history'
        
    def __str__(self):
        return f"InvoiceHistory {self.so_number} - {self.status}"
    
    
    
    
class InvoiceLog(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('EDITED' , 'Edited'),
        ('ERROR', 'Error'),
        ('POSTED_TO_SAP' , 'Posted to SAP'),
        ('CL_RAISED' , 'CL Raised')
    ]
    
    so_number = models.CharField(max_length=100)
    party_name = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    branch = models.CharField(max_length=25 , blank=True , null=True)
    warehouse = models.CharField(max_length=25 , blank=True , null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    rejection_reason = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    invoice_payload = models.JSONField()

    # The rejected log this one replaces, set when a reviewer reworks an invoice
    # through the Edit action. Gives the approver of the replacement the history
    # of why the previous attempt was turned down.
    supersedes = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='superseded_by',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='invoice_logs')

    class Meta:
        db_table = 'invoice_log'

    def __str__(self):
        return f"InvoiceLog {self.so_number} - {self.status}"

    def revision_chain(self):
        """This log and every earlier version it descends from, oldest first.

        Guarded against a cycle: `supersedes` is only ever set to a pre-existing
        row so a loop should be impossible, but a bad backfill must not hang a
        request.
        """
        chain = []
        seen = set()
        node = self
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            chain.append(node)
            node = node.supersedes
        chain.reverse()
        return chain
    
    def save(self, *args, **kwargs):
        if self.status == 'REJECTED' and not self.rejection_reason:
            self.error_message = 'Invoice rejected without specific reason.'
        
        if self.status == 'ERROR' and not self.error_message:
            self.error_message = 'An error occurred during invoice processing.'
            
        is_new = self.pk is None # Optional tracking variable if you only want to know if it's new
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

class CreditLimitLogs(models.Model):
    invoice_log = models.ForeignKey(InvoiceLog , on_delete=models.CASCADE,  primary_key=True)
    jsap_doc_id = models.IntegerField()
    party_name = models.CharField(max_length=255)
    # credit_raised = models.DecimalField(max_digits=10 , decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        db_table = 'credit_limit_logs'
