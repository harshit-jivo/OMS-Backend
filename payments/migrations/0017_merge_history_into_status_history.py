"""Fold the approval and SAP-posting logs into payment_status_history.

The module is moving to ONE history table. Before the old ones are dropped,
their rows are copied across so the timeline keeps the past rather than
starting empty — a payment approved last week must still show who approved it.

Runs before 0018, which does the dropping.
"""
from django.db import migrations

# ApprovalAction.action -> PaymentStatusHistory.Action
ACTION_MAP = {
    'SUBMIT': 'SUBMITTED',
    'RESUBMIT': 'RESUBMITTED',
    'APPROVE': 'APPROVED',
    'REJECT': 'REJECTED',
    'CANCEL': 'CANCELLED',
    'RETURN': 'RETURNED',
}

# SapPostingHistory.action -> PaymentStatusHistory.Action
SAP_MAP = {
    'POST_STARTED': 'SAP_POST_STARTED',
    'POST_SUCCESS': 'SAP_POSTED',
    'POST_FAILED': 'SAP_FAILED',
    'POST_TIMEOUT': 'SAP_UNKNOWN',
    'RESUBMITTED': 'RESUBMITTED',
}


def merge(apps, schema_editor):
    History = apps.get_model('payments', 'PaymentStatusHistory')
    Receipt = apps.get_model('payments', 'PaymentReceipt')
    Deposit = apps.get_model('payments', 'BankDeposit')
    SapHistory = apps.get_model('payments', 'SapPostingHistory')
    ContentType = apps.get_model('contenttypes', 'ContentType')

    try:
        ApprovalAction = apps.get_model('approvals', 'ApprovalAction')
        ApprovalRequest = apps.get_model('approvals', 'ApprovalRequest')
    except LookupError:                                   # already removed
        ApprovalAction = ApprovalRequest = None

    receipt_ct = ContentType.objects.get_for_model(Receipt)
    deposit_ct = ContentType.objects.get_for_model(Deposit)

    rows = []

    # --- SAP posting attempts -------------------------------------------
    # `payment` is a FK to PaymentReceipt, so every row is a receipt event.
    for entry in SapHistory.objects.all().iterator():
        rows.append(History(
            content_type_id=receipt_ct.id,
            object_id=entry.payment_id,
            action=SAP_MAP.get(entry.action, 'STATUS_CHANGED'),
            from_status='',
            to_status=entry.status or '',
            reason=(entry.sap_response or '')[:4000],
            actor_kind='SAP',
            sap_doc_entry=entry.sap_doc_entry,
            sap_doc_num=entry.sap_doc_num,
            changed_by_id=entry.created_by_id,
            changed_by_username=entry.created_by_username or '',
            created_at=entry.created_at,
        ))

    # --- Approval decisions ----------------------------------------------
    if ApprovalAction is not None:
        # document_number is the only link back — the request is generic, and
        # a receipt number and a deposit number never collide (RCP- vs DEP-).
        receipts = {r.receipt_no: r.id for r in Receipt.objects.all()}
        deposits = {d.deposit_no: d.id for d in Deposit.objects.all()}

        requests = {r.id: r for r in ApprovalRequest.objects.all()}
        for act in ApprovalAction.objects.all().iterator():
            req = requests.get(act.request_id)
            if req is None:
                continue
            number = req.document_number
            if number in receipts:
                ct_id, obj_id = receipt_ct.id, receipts[number]
            elif number in deposits:
                ct_id, obj_id = deposit_ct.id, deposits[number]
            else:
                continue                       # document already deleted
            rows.append(History(
                content_type_id=ct_id,
                object_id=obj_id,
                action=ACTION_MAP.get(act.action, 'STATUS_CHANGED'),
                from_status='',
                to_status='',
                reason=act.remarks or '',
                actor_kind='USER',
                level=act.level,
                level_label=act.level_name or '',
                changed_by_id=act.approver_id,
                changed_by_username=act.approver_username or '',
                created_at=act.acted_at,
            ))

    if rows:
        History.objects.bulk_create(rows, batch_size=500)


def unmerge(apps, schema_editor):
    """Remove only the rows this migration created.

    Identified by actor_kind='SAP' or by an approval action — the statuses the
    original table wrote were all STATUS_CHANGED with actor_kind USER/SYSTEM.
    """
    History = apps.get_model('payments', 'PaymentStatusHistory')
    History.objects.filter(actor_kind='SAP').delete()
    History.objects.filter(
        action__in=list(ACTION_MAP.values()),
        level__isnull=False,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0016_paymentstatushistory_action_and_more'),
        # The `approvals` app was REMOVED once payments moved to the
        # Workflow Engine. Its dependency edge is dropped with it —
        # Django only needs the graph to resolve, and an applied
        # migration is never re-run. The body above already tolerates
        # the models being absent (`LookupError`), so this migration
        # still applies cleanly to a fresh database.
    ]

    operations = [
        migrations.RunPython(merge, unmerge),
    ]
