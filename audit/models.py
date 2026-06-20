from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """A simple record of a change made on an admin page.

    One row per changed field, so the old and new values sit in their own
    columns: who / page / action / record / field / old value / new value /
    when.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='audit_logs',
    )
    # Snapshot of the username so the log survives the user being deleted.
    username = models.CharField(max_length=150, blank=True, default='')

    page = models.CharField(max_length=100, blank=True, default='')
    action = models.CharField(max_length=20, blank=True, default='')   # Created / Updated / Deleted
    record = models.CharField(max_length=255, blank=True, default='')  # which record changed

    field = models.CharField(max_length=100, blank=True, default='')   # which field changed
    old_value = models.TextField(blank=True, default='')
    new_value = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'audit_log'
        ordering = ['-created_at']
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'

    def __str__(self):
        who = self.username or 'system'
        if self.field:
            return f'{who} {self.action} {self.record}.{self.field}'
        return f'{who} {self.action} {self.record or self.page}'
