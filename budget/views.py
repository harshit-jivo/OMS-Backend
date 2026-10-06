"""BUDGET API (`/api/budget/`).

No create and no edit: SAP is the origin. Drafts arrive through
`manage.py sync_budget_drafts` only. Identity is always `request.user`.

    GET  queue/                  items waiting for me today (stand-ins included)
    GET  history/                items I decided
    GET  drafts/?company=&status=   every draft OMS holds
    GET  drafts/<id>/            one draft: its lines, items, their stages and history
    POST items/<id>/approve/     {remarks, version}
    POST items/<id>/reject/      {remarks, version}  — remarks required
    POST items/<id>/retry-sap/   re-attempt a failed write to SAP's gate
    GET/PUT settings/            auto-approval (Budget_Settings)
    GET  health/                 last sync per company; pending and failed counts
    GET  users/                  active users, for the exempt-users picker (Budget_Settings)
"""
import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.responses import fail, ok

from budget import permissions as budget_perms
from budget.models import BudgetDraft, BudgetItem, BudgetSettings, DraftStatus, ItemStatus
from budget.serializers import draft_data, item_data
from budget.services import flow as flow_service
from budget.services import sap as sap_service

logger = logging.getLogger(__name__)

LIST_LIMIT = 500


def _items():
    return (BudgetItem.objects.select_related('draft', 'workflow', 'current_stage', 'current_user')
            .prefetch_related('lines'))


class _Desk(APIView):
    def get_permissions(self):
        return [IsAuthenticated(), budget_perms.CanUseDesk()]


class QueueView(_Desk):
    def get(self, request):
        ids = list(flow_service.pending_for(request.user).values_list('id', flat=True))
        rows = _items().filter(pk__in=ids).order_by('waiting_since')
        return ok([item_data(i, user=request.user) for i in rows])


class HistoryView(_Desk):
    def get(self, request):
        rows = _items().filter(pk__in=flow_service.decided_item_ids(request.user)).order_by('-updated_at')
        return ok([item_data(i, user=request.user) for i in rows[:LIST_LIMIT]])


class DraftListView(_Desk):
    def get(self, request):
        qs = BudgetDraft.objects.all()
        company = (request.query_params.get('company') or '').strip().upper()
        if company:
            qs = qs.filter(company=company)
        status = (request.query_params.get('status') or '').strip().upper()
        if status:
            if status not in DraftStatus.values:
                return fail('`status` must be one of ' + ', '.join(DraftStatus.values),
                            status=http_status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(status=status)
        return ok([draft_data(d) for d in qs.order_by('-created_at')[:LIST_LIMIT]])


class DraftDetailView(_Desk):
    def get(self, request, pk):
        draft = BudgetDraft.objects.filter(pk=pk).first()
        if draft is None:
            return fail('Draft not found.', status=http_status.HTTP_404_NOT_FOUND)
        items = _items().filter(draft=draft).order_by('created_at')
        return ok({**draft_data(draft), 'items': [item_data(i, user=request.user, detail=True) for i in items]})


class _Decision(_Desk):
    def _load(self, request, pk):
        item = _items().filter(pk=pk).first()
        if item is None:
            return None, fail('Not found.', status=http_status.HTTP_404_NOT_FOUND)
        allowed, reason = flow_service.may_act_on(request.user, item)
        if not allowed:
            code = http_status.HTTP_409_CONFLICT if item.status != ItemStatus.PENDING else http_status.HTTP_403_FORBIDDEN
            return None, fail(reason, status=code)
        return item, None

    def _write(self, item, gate, remarks, user):
        """Write SAP's gate after the decision committed. A failure is reported, never undoes it."""
        try:
            sap_service.write_item(item, gate, decided_by=user.username, remarks=remarks)
            return True, ''
        except (sap_service.SapWriteError, sap_service.SapUnavailable) as exc:
            return False, str(exc)


class ApproveView(_Decision):
    def post(self, request, pk):
        item, error = self._load(request, pk)
        if error:
            return error
        remarks = (request.data.get('remarks') or '').strip()
        try:
            item, gate = flow_service.approve(item, user=request.user, remarks=remarks,
                                              version=request.data.get('version'))
        except flow_service.BudgetFlowError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)
        written, why = self._write(item, gate, remarks, request.user)
        item = _items().get(pk=item.pk)
        if item.status == ItemStatus.APPROVED:
            message = ('Approved. SAP will now let these lines be posted.' if written else
                       f'Approved, but SAP did not take it ({why}). The approval stands and can be retried.')
        else:
            message = f'Approved. It now waits at {item.current_stage.name if item.current_stage else "the next stage"}.'
        return ok(item_data(item, user=request.user, detail=True), message=message)


class RejectView(_Decision):
    def post(self, request, pk):
        item, error = self._load(request, pk)
        if error:
            return error
        remarks = (request.data.get('remarks') or '').strip()
        try:
            item = flow_service.reject(item, user=request.user, remarks=remarks,
                                       version=request.data.get('version'))
        except flow_service.BudgetFlowError as exc:
            return fail(str(exc), status=http_status.HTTP_409_CONFLICT)
        written, why = self._write(item, sap_service.REJECTED, remarks, request.user)
        message = ('Rejected. SAP keeps refusing to post these lines; if the draft is changed in SAP it comes '
                   'back for approval.' if written else
                   f'Rejected, but SAP did not record it ({why}). The lines stay blocked regardless.')
        return ok(item_data(_items().get(pk=item.pk), user=request.user, detail=True), message=message)


class RetrySapView(_Desk):
    def post(self, request, pk):
        item = _items().filter(pk=pk).first()
        if item is None:
            return fail('Not found.', status=http_status.HTTP_404_NOT_FOUND)
        gate = {ItemStatus.APPROVED: sap_service.APPROVED, ItemStatus.REJECTED: sap_service.REJECTED}.get(item.status)
        if gate is None:
            return fail('Only an approved or rejected item is written to SAP.', status=http_status.HTTP_409_CONFLICT)
        if item.sap_status == 'SUCCESS':
            return fail('This decision has already reached SAP.', status=http_status.HTTP_409_CONFLICT)
        try:
            text = sap_service.write_item(item, gate, decided_by=request.user.username)
        except (sap_service.SapWriteError, sap_service.SapUnavailable) as exc:
            return fail(str(exc), status=http_status.HTTP_502_BAD_GATEWAY)
        return ok({'sap_status': 'SUCCESS', 'sap_status_text': text}, message='SAP accepted it.')


def _settings_data(row):
    return {
        'auto_approve_enabled': row.auto_approve_enabled, 'auto_approve_hours': row.auto_approve_hours,
        'exempt_users': [{'id': u.pk, 'name': getattr(u, 'name', '') or u.username, 'username': u.username}
                         for u in row.exempt_users.order_by('name', 'username')],
        'last_sync': row.last_sync or {}, 'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


class SettingsView(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), budget_perms.CanUseDesk()]
        return [IsAuthenticated(), budget_perms.CanChangeSettings()]

    def get(self, request):
        return ok(_settings_data(BudgetSettings.load()))

    @transaction.atomic
    def put(self, request):
        row = BudgetSettings.load()
        data = request.data or {}
        hours = data.get('auto_approve_hours', row.auto_approve_hours)
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            return fail('Hours must be a whole number.', status=http_status.HTTP_400_BAD_REQUEST)
        if not 1 <= hours <= 720:
            return fail('Hours must be between 1 and 720.', status=http_status.HTTP_400_BAD_REQUEST)
        row.auto_approve_enabled = bool(data.get('auto_approve_enabled', row.auto_approve_enabled))
        row.auto_approve_hours = hours
        row.updated_by = request.user
        row.save()
        if 'exempt_user_ids' in data:
            ids = [int(i) for i in data.get('exempt_user_ids') or [] if str(i).isdigit()]
            row.exempt_users.set(get_user_model().objects.filter(pk__in=ids))
        return ok(_settings_data(row), message='Saved.')


class UsersView(APIView):
    """Active users, for the settings page's exempt-users picker. `[{id, name, username}]`."""

    def get_permissions(self):
        return [IsAuthenticated(), budget_perms.CanChangeSettings()]

    def get(self, request):
        rows = get_user_model().objects.filter(is_active=True).order_by('name', 'username')
        return ok([{'id': u.pk, 'name': getattr(u, 'name', '') or u.username, 'username': u.username}
                   for u in rows[:1000]])


class HealthView(_Desk):
    def get(self, request):
        last = BudgetSettings.load().last_sync or {}
        out = []
        for company in sorted(sap_service.schemas()):
            out.append({
                'company': company, 'last_synced_at': last.get(company),
                'pending': BudgetItem.objects.filter(company=company, status=ItemStatus.PENDING).count(),
                'sap_write_failed': BudgetItem.objects.filter(company=company, sap_status='FAILED').count(),
            })
        return ok(out)
