"""Seed the document tracker's stages and lookup dropdowns.

Idempotent — safe to run repeatedly. Uses update_or_create keyed on the
natural key so re-running only tops up / corrects rows without duplicating.

    python manage.py seed_tracker
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from tracker.models import (
    Branch, Category, GstRate, GstType, InvoiceMode, Stage, Unit,
)

# --- The flow (Bilty/GRPO merged with Tiwari ji receiving) ---
STAGES = [
    dict(code='entry',        name='Invoice Entry',      order=1,
         status_choices=[],                                   requires_status=False,
         can_return=False, is_terminal=False, threshold_days=2),
    dict(code='bilty_grpo',   name='Bilty / GRPO',       order=2,
         status_choices=[],                                   requires_status=False,
         can_return=True,  is_terminal=False, threshold_days=3),
    dict(code='pre_audit',    name='Pre-Audit',          order=3,
         status_choices=['OK', 'HOLD', 'DEBIT', 'RETURN'],    requires_status=True,
         can_return=True,  is_terminal=False, threshold_days=3),
    dict(code='data_entry',   name='Data Entry',         order=4,
         status_choices=[],                                   requires_status=False,
         can_return=True,  is_terminal=False, threshold_days=2),
    # SAP and JSAP are two separate desks. SAP approval is a person clicking
    # Approve/Reject here; JSAP approval is the budget decision made in the
    # JSAP system, which this stage only mirrors (see tracker/jsap.py).
    dict(code='sap_approval', name='SAP Approval',       order=5,
         status_choices=['APPROVED', 'REJECTED'],             requires_status=True,
         can_return=True,  is_terminal=False, threshold_days=3),
    dict(code='jsap_approval', name='JSAP Approval',     order=6,
         status_choices=['APPROVED', 'REJECTED'],             requires_status=True,
         can_return=True,  is_terminal=False, threshold_days=3),
    dict(code='save_in_sap',  name='Save in SAP',        order=7,
         status_choices=[],                                   requires_status=False,
         can_return=True,  is_terminal=False, threshold_days=2),
    dict(code='payment',      name='Payment',            order=8,
         status_choices=[],                                   requires_status=False,
         can_return=False, is_terminal=True,  threshold_days=5),
]

CATEGORIES = [
    'Transport', 'RM-PM', 'Contractor', 'Fixed Asset', 'Security',
    'Other Invoice/Consumable', 'Cash Voucher', 'Staff Imprest',
    'Refreshment', 'E-COM',
]
UNITS = ['Oil', 'Beverage']
BRANCHES = ['Wellness', 'Mart']
MODES = ['Mail', 'Hardcopy']
GST_TYPES = ['SGST-CGST', 'IGST', 'ISD', 'NO GST']
GST_RATES = [('0%', 0), ('5%', 5), ('12%', 12), ('18%', 18), ('28%', 28)]


class Command(BaseCommand):
    help = 'Seed tracker stages and lookup dropdown values (idempotent).'

    @transaction.atomic
    def handle(self, *args, **options):
        # Stage.order is unique, so re-seeding an existing flow whose positions
        # shifted (e.g. the SAP/JSAP split pushing later stages down one) would
        # collide mid-loop. Park every current order out of range first.
        for offset, stage in enumerate(Stage.objects.order_by('-order')):
            Stage.objects.filter(pk=stage.pk).update(order=1000 + offset)
        for s in STAGES:
            Stage.objects.update_or_create(code=s['code'], defaults=s)

        for i, name in enumerate(CATEGORIES):
            Category.objects.update_or_create(name=name, defaults={'sort_order': i})
        for i, name in enumerate(UNITS):
            Unit.objects.update_or_create(name=name, defaults={'sort_order': i})
        for i, name in enumerate(BRANCHES):
            Branch.objects.update_or_create(name=name, defaults={'sort_order': i})
        for i, name in enumerate(MODES):
            InvoiceMode.objects.update_or_create(name=name, defaults={'sort_order': i})
        for i, name in enumerate(GST_TYPES):
            GstType.objects.update_or_create(name=name, defaults={'sort_order': i})
        for i, (label, rate) in enumerate(GST_RATES):
            GstRate.objects.update_or_create(
                rate=rate, defaults={'label': label, 'sort_order': i},
            )

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {len(STAGES)} stages and all lookup values.'
        ))
