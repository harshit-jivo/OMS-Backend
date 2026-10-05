"""Copy the remaining approval decisions into payments' own history.

WHY THIS MUST RUN BEFORE THE OLD TABLES ARE DROPPED
---------------------------------------------------
`0017_merge_history_into_status_history` already did this once, on 2026-08-07.
But the old engine kept recording decisions for another month, and the live
code path did NOT reliably write them to `payment_status_history` as well: an
approval left `approval_action` rows plus anonymous STATUS_CHANGED rows here,
so the timeline recorded that something changed without recording WHO approved
it.

Measured on TEST before writing this: of 165 approval actions, 75 APPROVE /
REJECT decisions across 75 documents exist ONLY in the old tables. One real,
posted payment:

    approval_action                 payment_status_history
      SUBMIT   by gagan1              STATUS_CHANGED  by gagan1
      APPROVE  by kamal               STATUS_CHANGED  by gagan1
      APPROVE  by Navdeep             SAP_POST_STARTED
                                      SAP_POSTED

Dropping `approval_action` without this migration would permanently destroy the
record that kamal and Navdeep authorised that money. That is financial
approval history, and nothing else in the system holds it.

WHAT IT WRITES
--------------
One `PaymentStatusHistory` row per approval action that has no counterpart,
carrying the action, the approver's username, their remarks, the level and the
ORIGINAL timestamp — so the decision appears in the timeline at the moment it
actually happened rather than at the moment this migration ran.

IDEMPOTENT. An action is skipped when a matching row already exists within five
minutes of it, which is how the twelve already-present ones were identified.
Re-running therefore adds nothing: the rows this writes match themselves
exactly.

NOT REVERSED. The backward pass is a no-op, deliberately. These rows are the
only surviving copy of data the next migration deletes; "undoing" this by
removing them again would destroy it a second time, which is the opposite of
what a rollback is for.
"""
from datetime import timedelta

from django.db import migrations

#: ApprovalAction.action -> PaymentStatusHistory.Action. The same mapping
#: `0017_merge_history_into_status_history` used, kept identical so a decision
#: reads the same whichever migration copied it.
ACTION_MAP = {
    'SUBMIT': 'SUBMITTED',
    'RESUBMIT': 'RESUBMITTED',
    'APPROVE': 'APPROVED',
    'REJECT': 'REJECTED',
    'CANCEL': 'CANCELLED',
    'RETURN': 'RETURNED',
}

#: How close an existing row must be to count as the same event. The live code
#: wrote its row seconds either side of the engine's, never minutes.
SAME_EVENT_WINDOW = timedelta(minutes=5)


def _forward(apps, schema_editor):
    History = apps.get_model('payments', 'PaymentStatusHistory')
    Receipt = apps.get_model('payments', 'PaymentReceipt')
    Deposit = apps.get_model('payments', 'BankDeposit')
    ContentType = apps.get_model('contenttypes', 'ContentType')

    try:
        ApprovalAction = apps.get_model('approvals', 'ApprovalAction')
        ApprovalRequest = apps.get_model('approvals', 'ApprovalRequest')
    except LookupError:
        # The app is already gone — nothing to preserve, and nothing to fail
        # over. This is what makes the migration safe to run on a database
        # that never had the old engine.
        return

    receipt_ct = ContentType.objects.get_for_model(Receipt)
    deposit_ct = ContentType.objects.get_for_model(Deposit)

    # `document_number` is the only link back: the request is generic, and a
    # receipt number never collides with a deposit number (RCP- vs DEP-).
    receipts = {r.receipt_no: r.id for r in Receipt.objects.all()}
    deposits = {d.deposit_no: d.id for d in Deposit.objects.all()}
    requests = {r.id: r for r in ApprovalRequest.objects.all()}

    rows = []
    for act in ApprovalAction.objects.all().iterator():
        request = requests.get(act.request_id)
        if request is None:
            continue

        number = request.document_number
        if number in receipts:
            ct_id, object_id = receipt_ct.id, receipts[number]
        elif number in deposits:
            ct_id, object_id = deposit_ct.id, deposits[number]
        else:
            # The document itself is gone; there is no timeline to add to.
            continue

        action = ACTION_MAP.get(act.action, 'STATUS_CHANGED')
        already = History.objects.filter(
            content_type_id=ct_id,
            object_id=object_id,
            action=action,
            created_at__gte=act.acted_at - SAME_EVENT_WINDOW,
            created_at__lte=act.acted_at + SAME_EVENT_WINDOW,
        ).exists()
        if already:
            continue

        rows.append(History(
            content_type_id=ct_id,
            object_id=object_id,
            action=action,
            from_status='',
            to_status='',
            reason=act.remarks or '',
            level=act.level,
            level_label=act.level_name or '',
            # The approver, by username. The history stores only the name on
            # purpose — an audit row must survive the account being deleted.
            changed_by_username=act.approver_username or '',
        ))
        # Carried alongside rather than on the field: see the note by the
        # insert below — `auto_now_add` discards anything set here.
        rows[-1]._acted_at = act.acted_at

    if not rows:
        return

    # `created_at` is `auto_now_add`, which OVERRIDES whatever the instance
    # carries — including through `bulk_create`. Inserting alone therefore
    # stamps every preserved decision with the moment the migration ran, which
    # is worse than useless: the timeline would claim a payment approved in
    # August was approved during the upgrade.
    #
    # `QuerySet.update()` is the one write path `auto_now_add` does not touch,
    # so the real timestamps are restored immediately after the insert, inside
    # the same migration.
    created = History.objects.bulk_create(rows, batch_size=500)
    for row, action in zip(created, [r._acted_at for r in rows]):
        History.objects.filter(pk=row.pk).update(created_at=action)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0037_repair_payments_doc_no_alias'),
        # The `approvals` app was REMOVED once payments moved to the
        # Workflow Engine. Its dependency edge is dropped with it —
        # Django only needs the graph to resolve, and an applied
        # migration is never re-run. The body above already tolerates
        # the models being absent (`LookupError`), so this migration
        # still applies cleanly to a fresh database.
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(_forward, migrations.RunPython.noop),
    ]
