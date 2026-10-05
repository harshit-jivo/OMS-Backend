"""Restore the real decision times on the rows 0038 preserved.

WHAT WENT WRONG
---------------
`PaymentStatusHistory.created_at` is `auto_now_add`, which overrides whatever
the instance carries — including through `bulk_create`. The first run of
`0038_preserve_approval_decisions` therefore inserted every preserved decision
correctly EXCEPT its timestamp: 153 approvals made across August and early
September were all stamped with the moment the migration ran.

That is worse than a cosmetic defect. The timeline would say a payment was
approved during an upgrade rather than when it was authorised, and every
preserved decision would sort to the bottom of the document's history, below
the SAP posting it actually preceded.

0038 has since been corrected to re-stamp its own rows, so a database that has
not run it yet gets the right timestamps first time and this migration finds
nothing to do. This exists for the one that already ran.

HOW A ROW IS PAIRED WITH ITS DECISION
-------------------------------------
By content, not by id: the document, the action, and the approver's username.
Where the same person decided the same document twice — a retry, a second
round — both sides are ordered and zipped, so the earlier row takes the earlier
time.

A row is only touched when its timestamp is more than five minutes from the
decision it belongs to. Rows the live code wrote at the right time are already
within seconds of it and are left exactly as they are.

REVERSIBLE: no. The backward pass is a no-op. Restoring the wrong timestamps
would serve nobody, and the right ones are a faithful copy of what the old
tables recorded.
"""
from collections import defaultdict
from datetime import timedelta

from django.db import migrations

ACTION_MAP = {
    'SUBMIT': 'SUBMITTED',
    'RESUBMIT': 'RESUBMITTED',
    'APPROVE': 'APPROVED',
    'REJECT': 'REJECTED',
    'CANCEL': 'CANCELLED',
    'RETURN': 'RETURNED',
}

#: Farther apart than this and the row is not recording the same moment.
DRIFT = timedelta(minutes=5)


def _forward(apps, schema_editor):
    History = apps.get_model('payments', 'PaymentStatusHistory')
    Receipt = apps.get_model('payments', 'PaymentReceipt')
    Deposit = apps.get_model('payments', 'BankDeposit')
    ContentType = apps.get_model('contenttypes', 'ContentType')

    try:
        ApprovalAction = apps.get_model('approvals', 'ApprovalAction')
        ApprovalRequest = apps.get_model('approvals', 'ApprovalRequest')
    except LookupError:
        # Nothing left to pair against — the source of truth is gone, so the
        # timestamps stay as they are rather than being guessed at.
        return

    receipt_ct = ContentType.objects.get_for_model(Receipt)
    deposit_ct = ContentType.objects.get_for_model(Deposit)
    receipts = {r.receipt_no: r.id for r in Receipt.objects.all()}
    deposits = {d.deposit_no: d.id for d in Deposit.objects.all()}
    requests = {r.id: r for r in ApprovalRequest.objects.all()}

    # key -> the decisions it covers, oldest first
    decisions = defaultdict(list)
    for act in ApprovalAction.objects.all().order_by('acted_at', 'id'):
        request = requests.get(act.request_id)
        if request is None:
            continue
        number = request.document_number
        if number in receipts:
            ct_id, object_id = receipt_ct.id, receipts[number]
        elif number in deposits:
            ct_id, object_id = deposit_ct.id, deposits[number]
        else:
            continue
        key = (ct_id, object_id,
               ACTION_MAP.get(act.action, 'STATUS_CHANGED'),
               act.approver_username or '')
        decisions[key].append(act.acted_at)

    if not decisions:
        return

    # key -> the history rows that claim to record them, in insertion order
    rows = defaultdict(list)
    for row in History.objects.filter(
            action__in=set(ACTION_MAP.values())).order_by('id'):
        key = (row.content_type_id, row.object_id, row.action,
               row.changed_by_username or '')
        if key in decisions:
            rows[key].append(row)

    for key, moments in decisions.items():
        for row, moment in zip(rows.get(key, []), moments):
            if abs(row.created_at - moment) <= DRIFT:
                continue                      # already right; leave it alone
            # `update()` is the one write path `auto_now_add` does not touch.
            History.objects.filter(pk=row.pk).update(created_at=moment)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0038_preserve_approval_decisions'),
        # The `approvals` app was REMOVED once payments moved to the
        # Workflow Engine. Its dependency edge is dropped with it —
        # Django only needs the graph to resolve, and an applied
        # migration is never re-run. The body above already tolerates
        # the models being absent (`LookupError`), so this migration
        # still applies cleanly to a fresh database.
    ]

    operations = [
        migrations.RunPython(_forward, migrations.RunPython.noop),
    ]
