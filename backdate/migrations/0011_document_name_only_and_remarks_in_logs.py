"""One document identity, and no remarks column on the request.

WHAT CHANGES AND WHY
--------------------
1. `backdate.document_type` (the numeric `MOBJ.ObjType`) is DROPPED.
   `document_type_name` becomes the single stored document identity.

   `OPEN_BKDT` still needs the NUMBER — its `TRANSTYPE` parameter is an
   INTEGER — so it is resolved from the name against `MOBJ` at the moment of
   the SAP call. That round trip is exact, not a guess: `MOBJ` holds 75
   objects with 75 distinct, non-blank names, identical across all three
   company schemas (verified against live HANA, 2026-09-15). The SAP contract
   is unchanged: 11 parameters, same order, no ACTION parameter.

   Existing rows raised before `document_type_name` existed carry a blank
   name, so the number is translated into a name BEFORE the column goes. The
   map below is that same verified `MOBJ` snapshot, written out in full so
   this migration is deterministic and needs no database but its own.

2. `backdate.remarks` is DROPPED.
   A remark is EVENT-specific — the reason for raising, the reason for an
   edit, an approver's note, a rejection reason — by up to four different
   people. One column can hold only the last of them, and overwriting a
   rejection reason with a later note is how an audit trail stops being true.
   Every remark is a row in `backdate_action_logs`, which already has the
   column and is already append-only.

   Any remark still sitting on a request is copied to its CREATE log row
   first, so nothing is lost. In this database all four already match, because
   `flow.submit` has been writing the CREATE log from that column all along —
   the copy is for environments where that is not so.

NOT TOUCHED: the Workflow Engine, `backdate_flow` (whose `sap_payload` jsonb
column already exists, added in 0004), and the action-log structure.
"""
from django.db import migrations, models

#: `MOBJ` as it reads in every company schema — `ObjType` -> `ObjName`.
#: Verified live: 75 rows, 75 distinct names, none blank, identical in
#: JIVO_OIL_HANADB, JIVO_BEVERAGES_HANADB and JIVO_MART_HANADB.
MOBJ = {
    1: 'G/L Accounts', 2: 'Business Partner', 3: 'Bank Codes', 4: 'Items',
    5: 'Tax Definition', 6: 'Price Lists', 7: 'Special Prices',
    8: 'Item Properties', 9: 'Rate Differences', 10: 'Card Groups',
    11: 'Contact Persons', 12: 'Users', 13: 'A/R Invoice',
    14: 'A/R Credit Memo', 15: 'Delivery', 16: 'Returns', 17: 'Sales Order',
    18: 'A/P Invoice', 19: 'A/P Credit Memo', 20: 'Goods Receipt PO',
    21: 'Goods Return', 22: 'Purchase Order', 23: 'Sales Quotation',
    24: 'Incoming Payment', 25: 'Deposit', 26: 'Reconciliation History',
    27: 'Check Register', 28: 'Journal Voucher Entry',
    29: 'Journal Vouchers List', 30: 'Journal Entry', 31: 'Items - Warehouse',
    32: 'Print Preferences', 33: 'Activities', 34: 'Recurring Postings',
    35: 'Document Numbering', 36: 'Credit Cards', 37: 'Currency Codes',
    38: 'CPI Codes', 39: 'Administration', 40: 'Payment Terms',
    41: 'Preferences', 42: 'External Bank Statement Received',
    43: 'Manufacturers', 44: 'Card Properties', 45: 'Journal Entry Codes',
    46: 'Outgoing Payments', 47: 'Serial Numbers', 48: 'Loading Expenses',
    49: 'Delivery Types', 50: 'Length Units', 51: 'Weight Units',
    52: 'Item Groups', 53: 'Sales Employee',
    54: 'Report - Selection Criteria', 55: 'Posting Templates',
    56: 'Customs Groups', 57: 'Checks for Payment', 58: 'Whse Journal',
    59: 'Goods Receipt', 60: 'Goods Issue', 61: 'Cost Center',
    62: 'Cost Rate', 63: 'Project Codes', 64: 'Warehouses',
    65: 'Commission Groups', 66: 'Product Tree', 67: 'Inventory Transfer',
    68: 'Production Instructions', 69: 'Landed Costs', 70: 'Payment Methods',
    71: 'Credit Card Payment', 72: 'Credit Card Management',
    73: 'Customer/Vendor Cat. No.', 74: 'Credit Payments',
    75: 'CPI and FC Rates',
}


def name_the_documents(apps, schema_editor):
    """Give every row a name before the number it came from disappears."""
    BackDate = apps.get_model('backdate', 'BackDate')
    for row in BackDate.objects.all().only('id', 'document_type',
                                           'document_type_name'):
        if (row.document_type_name or '').strip():
            continue
        # A type outside the snapshot keeps its number as its name rather than
        # being blanked: unreadable is better than lost, and it still tells an
        # operator exactly what to correct.
        row.document_type_name = (MOBJ.get(row.document_type)
                                  or f'Object type {row.document_type}')[:120]
        row.save(update_fields=['document_type_name'])


def unname_the_documents(apps, schema_editor):
    """Reverse: recover the number from the name, best effort."""
    BackDate = apps.get_model('backdate', 'BackDate')
    numbers = {name.casefold(): number for number, name in MOBJ.items()}
    for row in BackDate.objects.all().only('id', 'document_type',
                                           'document_type_name'):
        row.document_type = numbers.get(
            (row.document_type_name or '').strip().casefold(), 0)
        row.save(update_fields=['document_type'])


def remarks_into_the_log(apps, schema_editor):
    """Move any remark still on a request onto its CREATE log row."""
    BackDate = apps.get_model('backdate', 'BackDate')
    BackDateActionLog = apps.get_model('backdate', 'BackDateActionLog')

    for row in BackDate.objects.exclude(remarks='').exclude(remarks=None):
        log = (BackDateActionLog.objects
               .filter(backdate_id=row.id, action='CREATE')
               .order_by('acted_at', 'id').first())
        if log is None:
            # No CREATE row to carry it (a request that predates the log, or
            # one whose submission failed). Write one rather than drop the
            # only thing the requester said.
            BackDateActionLog.objects.create(
                backdate_id=row.id, action='CREATE',
                acted_by_id=row.created_by_id, stage_id=None,
                remarks=row.remarks, action_data=None,
                acted_at=row.created_at)
        elif not (log.remarks or '').strip():
            log.remarks = row.remarks
            log.save(update_fields=['remarks'])


def remarks_back_onto_the_request(apps, schema_editor):
    """Reverse: copy the CREATE log's remark back onto the request."""
    BackDate = apps.get_model('backdate', 'BackDate')
    BackDateActionLog = apps.get_model('backdate', 'BackDateActionLog')

    for row in BackDate.objects.all():
        log = (BackDateActionLog.objects
               .filter(backdate_id=row.id, action='CREATE')
               .order_by('acted_at', 'id').first())
        row.remarks = (log.remarks if log else '') or ''
        row.save(update_fields=['remarks'])


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0010_single_company_and_document_name'),
    ]

    operations = [
        # Data first, in both directions: the columns must not disappear
        # until what they held is somewhere else.
        migrations.RunPython(name_the_documents, unname_the_documents),
        migrations.RunPython(remarks_into_the_log,
                             remarks_back_onto_the_request),

        # Now that every row has one, the name is required. It is the only
        # thing that says WHICH document the rights are for.
        migrations.AlterField(
            model_name='backdate',
            name='document_type_name',
            field=models.CharField(
                max_length=120,
                help_text='SAP object name (MOBJ.ObjName), e.g. "A/R Invoice".'),
        ),
        migrations.RemoveField(model_name='backdate', name='document_type'),
        migrations.RemoveField(model_name='backdate', name='remarks'),
    ]
