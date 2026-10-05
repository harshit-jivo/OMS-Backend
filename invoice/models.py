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
    # Which device the reviewer acted from, so the trail still reads the same
    # after that device is renamed or deactivated. Corroborating evidence only:
    # device_id is client-reported telemetry, so it backs up "who approved this",
    # it does not by itself prove it.
    #
    # These are written on every history row by `**describe_request_device(request)`
    # in invoice/views.py (three call sites), which is why migration
    # 0024_restore_invocie_history_device_columns puts them back after 0022
    # dropped them. Removing the fields while views.py still passes them raises
    #     TypeError: InvocieHistory() got unexpected keyword arguments:
    #     'device_id', 'device_name'
    device_id = models.CharField(max_length=64, blank=True, default='')
    device_name = models.CharField(max_length=150, blank=True, default='')

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
        # Set by the post-to-sap endpoint before it calls SAP, and cleared by
        # the answer. A log left here had no recorded answer (a timeout, a
        # killed worker), so the next attempt checks SAP before posting again.
        ('POSTING', 'Posting to SAP'),
        ('POSTED_TO_SAP' , 'Posted to SAP'),
        ('CL_RAISED' , 'CL Raised')
    ]

    # Statuses a reviewer may clear off the review screen.
    #
    # APPROVED is included, POSTED_TO_SAP is not, and the difference is whether
    # a real SAP document exists. An approved log is a decision OMS made and
    # has not yet acted on: measured on live, all 9 APPROVED logs carry no
    # `sap_doc_num`, while 92 of the 93 POSTED_TO_SAP ones do. Removing an
    # approved entry hides an OMS decision; removing a posted one would leave
    # OMS silent about an invoice SAP has actually issued.
    DELETABLE_STATUSES = ('PENDING', 'ERROR', 'REJECTED', 'EDITED', 'CL_RAISED',
                          'APPROVED')

    @property
    def has_sap_document(self):
        """Whether SAP has issued a document for this log.

        The real invariant behind `DELETABLE_STATUSES`, checked directly rather
        than inferred from the status. A status list is a proxy: it holds only
        while every posted log is labelled POSTED_TO_SAP, and a post that
        stamped the identifiers but failed to save the status would slip
        through it. This does not.
        """
        return bool((self.sap_doc_num or '').strip()
                    or (self.sap_doc_entry or '').strip())

    so_number = models.CharField(max_length=100)
    party_name = models.CharField(max_length=255)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    branch = models.CharField(max_length=25 , blank=True , null=True)
    warehouse = models.CharField(max_length=25 , blank=True , null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    rejection_reason = models.TextField(blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)
    invoice_payload = models.JSONField()

    # SAP identifiers of the invoice this log created, captured on a successful
    # post. `sap_doc_num` is the visible invoice number; `sap_doc_entry` is the
    # internal OINV key the Crystal bill print is actually rendered from. Kept as
    # text because SAP only guarantees them to be printable, not numeric.
    sap_doc_num = models.CharField(max_length=50, blank=True, null=True)
    sap_doc_entry = models.CharField(max_length=50, blank=True, null=True)

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

    # Soft delete. The review screen hides these rows, but the log and its
    # history survive: an invoice log is an audit record, and a reviewer
    # clearing clutter must not be able to destroy the trail of who submitted
    # what and why it was turned down. deleted_by is SET_NULL rather than
    # CASCADE so removing a user account does not take the deleted rows with it.
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_invoice_logs',
    )
    delete_reason = models.TextField(blank=True, null=True)

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
