"""Shared model base classes and the document-number generator.

The project has no timestamp/actor mixin — every model redeclares `created_at`
by hand, and coverage is uneven (`orders.OrderItem` has neither timestamp).
`TimeStampedModel` below fixes that for the new apps without touching existing
tables.
"""
from django.conf import settings
from django.db import models, transaction


class TimeStampedModel(models.Model):
    """created_at / updated_at / created_by, applied consistently.

    `created_by` is SET_NULL so history survives a user being removed, matching
    the convention used across the existing apps.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
    )

    class Meta:
        abstract = True


class DocumentCounter(models.Model):
    """Gapless per-scope sequence for receipt / deposit numbers.

    Replaces the read-then-write pattern used for order numbers
    (orders/views.py:2518-2529), which has no lock: two concurrent creates read
    the same "last" row and generate the same number, and since the column is
    unique one request then 500s.

    Allocation always goes through `next_number()`, which takes a row lock.
    """

    doc_type = models.CharField(max_length=20)          # RECEIPT | DEPOSIT
    company = models.CharField(max_length=20)           # OIL | BEVERAGES | MART
    fiscal_year = models.CharField(max_length=9)        # '2026-27' (Indian FY)
    prefix = models.CharField(max_length=20)            # RCP | DEP
    last_number = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_document_counter'
        constraints = [
            models.UniqueConstraint(
                fields=['doc_type', 'company', 'fiscal_year'],
                name='core_doc_counter_scope_uq',
            ),
        ]

    def __str__(self):
        return f'{self.prefix}-{self.company}-{self.fiscal_year}: {self.last_number}'


def fiscal_year_for(when):
    """Indian FY label for a date — April to March, e.g. '2026-27'."""
    year = when.year
    start = year if when.month >= 4 else year - 1
    return f'{start}-{str(start + 1)[-2:]}'


@transaction.atomic
def next_document_number(*, doc_type, company, prefix, when):
    """Allocate the next document number for a scope.

    MUST be called inside the caller's transaction so the number and the row
    that uses it commit together — otherwise a rolled-back create burns a
    number and leaves a visible gap in a financial sequence.

    Returns e.g. 'RCP-OIL-2026-27-000123'.
    """
    fy = fiscal_year_for(when)
    counter, _ = DocumentCounter.objects.select_for_update().get_or_create(
        doc_type=doc_type,
        company=company,
        fiscal_year=fy,
        defaults={'prefix': prefix},
    )
    counter.last_number += 1
    counter.save(update_fields=['last_number', 'updated_at'])
    return f'{prefix}-{company}-{fy}-{counter.last_number:06d}'
