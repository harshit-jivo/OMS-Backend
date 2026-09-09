"""Move each company's cash G/L onto a CASH payment-method mapping row.

`SapCompanyMap.cash_gl_account` is the last piece of that table that anything
needs. It belongs here: it is the account ONE TENDER posts to, exactly like the
`bank_key` every other tender already resolves through. Keeping it on a company
row made it look like a property of the company rather than of cash.

Copied from the live rows, never typed in — the values differ per company
(OIL and BEVERAGES use 1105001, MART uses 1105003) and inventing them would be
inventing an accounting entry.

The partial unique constraint `payment_method_mapping_one_active` already stops
a second active CASH row per company, so this is written as an update-or-create
against the active row and is safe to re-run.

Reverse deletes only the CASH rows this created and only while
`SapCompanyMap` still holds the values — after that table is dropped the
information exists nowhere else, so the reverse would be destructive rather
than reversible and is refused.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    SapCompanyMap = apps.get_model('payments', 'SapCompanyMap')
    PaymentMethodMapping = apps.get_model('payments', 'PaymentMethodMapping')

    for row in SapCompanyMap.objects.all():
        cash_gl = (row.cash_gl_account or '').strip()
        if not cash_gl:
            # Nothing to carry across. A company with no cash G/L could not
            # post a cash payment before this migration either, so writing a
            # blank row would only hide that.
            continue

        # The deposit source is the same drawer being emptied. Both columns
        # agree on every live row; a disagreement would mean the clearing
        # account never nets to zero, so it is surfaced rather than silently
        # resolved in favour of one of them.
        deposit_gl = (row.deposit_source_gl_account or '').strip()
        if deposit_gl and deposit_gl != cash_gl:
            raise RuntimeError(
                'Company %s has cash_gl_account=%r but '
                'deposit_source_gl_account=%r. These must be the same account '
                '— the deposit credits the drawer the receipt debited. '
                'Reconcile them before migrating.'
                % (row.company, cash_gl, deposit_gl))

        existing = PaymentMethodMapping.objects.filter(
            company=row.company, payment_method='CASH', is_active=True).first()
        if existing:
            existing.gl_account = cash_gl
            existing.save(update_fields=['gl_account', 'updated_at'])
        else:
            PaymentMethodMapping.objects.create(
                company=row.company,
                payment_method='CASH',
                bank_key='',            # a drawer is not a house bank
                gl_account=cash_gl,
                priority=0,
                is_active=True,
            )


def backwards(apps, schema_editor):
    SapCompanyMap = apps.get_model('payments', 'SapCompanyMap')
    PaymentMethodMapping = apps.get_model('payments', 'PaymentMethodMapping')

    if not SapCompanyMap.objects.exists():
        raise RuntimeError(
            'Refusing to remove the CASH mappings: SapCompanyMap no longer '
            'holds these G/L accounts, so deleting them would lose the only '
            'copy. Restore the company mapping rows first.')
    PaymentMethodMapping.objects.filter(payment_method='CASH').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0032_method_mapping_gl_account'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
