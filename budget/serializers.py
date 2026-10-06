"""Budget API shapes: plain dicts, as the pages read them."""
from workflow.services import selection
from workflow.services.assignments import get_stage_assignment

from budget.models import ItemStatus, LogAction
from budget.services import flow as flow_service


def _user(user):
    if user is None:
        return None
    return {'id': user.pk, 'name': getattr(user, 'name', '') or user.username, 'username': user.username}


def _iso(value):
    return value.isoformat() if value else None


def draft_data(draft):
    return {
        'id': draft.pk, 'company': draft.company, 'obj_type': draft.obj_type,
        'obj_type_label': draft.get_obj_type_display(), 'draft_entry': draft.draft_entry, 'doc_num': draft.doc_num,
        'doc_date': _iso(draft.doc_date), 'card_code': draft.card_code, 'card_name': draft.card_name,
        'sap_created_by': draft.sap_created_by, 'comments': draft.comments, 'status': draft.status,
        'synced_at': _iso(draft.synced_at), 'created_at': _iso(draft.created_at),
    }


def line_data(line):
    return {
        'line_num': line.line_num, 'acct_code': line.acct_code, 'acct_name': line.acct_name,
        'budget_code': line.budget_code, 'sub_budget_code': line.sub_budget_code,
        'effect_month': line.effect_month, 'amount': str(line.amount), 'remarks': line.remarks,
    }


def log_data(row):
    return {
        'action': row.action, 'label': row.get_action_display(), 'actor': _user(row.acted_by),
        'stage': row.stage.name if row.stage_id and row.stage else '', 'remarks': row.remarks,
        'data': row.action_data, 'at': _iso(row.acted_at),
    }


def stage_plan(item):
    """Every stage of the item's route, with where the item stands on it."""
    if item.workflow_id is None:
        return []
    decided = {}
    for row in item.action_logs.filter(action__in=[LogAction.APPROVE, LogAction.AUTO_APPROVE, LogAction.REJECT],
                                       stage__isnull=False):
        decided[row.stage_id] = row
    out = []
    for stage in selection.stages_for(item.workflow):
        row = decided.get(stage.id)
        if item.status == ItemStatus.PENDING and item.current_stage_id == stage.id:
            state = 'CURRENT'
        else:
            state = row.action if row else 'UPCOMING'
        assignment = get_stage_assignment(stage.id)
        out.append({'stage_id': stage.id, 'name': stage.name, 'sequence': stage.sequence, 'state': state,
                    'user_name': assignment.effective_username if assignment else '',
                    'acted_at': _iso(row.acted_at) if row else None})
    return out


def item_data(item, *, user=None, detail=False):
    allowed = bool(user) and flow_service.may_act_on(user, item)[0]
    out = {
        'id': item.pk, 'company': item.company, 'route': item.route, 'budget_code': item.budget_code,
        'amount': str(item.amount), 'status': item.status, 'status_label': item.get_status_display(),
        'version': item.version, 'workflow': item.workflow.code if item.workflow_id else None,
        'current_stage': item.current_stage.name if item.current_stage_id and item.current_stage else '',
        'current_user': _user(item.current_user), 'total_stage': item.total_stage,
        'waiting_since': _iso(item.waiting_since), 'sap_status': item.sap_status,
        'sap_status_text': item.sap_status_text, 'sap_written_at': _iso(item.sap_written_at),
        'draft': draft_data(item.draft), 'lines': [line_data(ln) for ln in item.lines.all()],
        'can': {'approve': allowed, 'reject': allowed,
                'retry_sap': item.status in (ItemStatus.APPROVED, ItemStatus.REJECTED)
                and item.sap_status == 'FAILED'},
    }
    if detail:
        out['stages'] = stage_plan(item)
        out['logs'] = [log_data(r) for r in item.action_logs.select_related('acted_by', 'stage')]
    return out
