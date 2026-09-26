"""Serializers for Advance Payment's own tables."""
import re

from rest_framework import serializers

from advance_payment.models import Employee

_PHONE = re.compile(r'^\+?[0-9][0-9\s-]{6,18}$')


class EmployeeSerializer(serializers.ModelSerializer):
    """The employee master, as the Add Employee page reads and writes it.

    Written by admins only (see `EmployeeMasterView`). `employee_id` is JSAP's
    id and optional: an employee added here may not be in JSAP yet.
    """

    role_label = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = Employee
        fields = [
            'id', 'employee_id', 'employee_code', 'employee_name', 'email', 'phone',
            'designation', 'role', 'role_label', 'gender', 'is_active', 'created_on',
        ]
        read_only_fields = ['id', 'created_on']
        extra_kwargs = {
            # DRF's own unique check would compare the code AS TYPED, before it
            # is upper-cased, so "jwpl0115" would pass it and then fail in the
            # database. `validate_employee_code` checks the upper-cased code.
            'employee_code': {'validators': []},
            'employee_id': {'required': False, 'allow_null': True},
            'email': {'required': False, 'allow_null': True, 'allow_blank': True},
            'phone': {'required': False, 'allow_null': True, 'allow_blank': True},
            'designation': {'required': False, 'allow_null': True, 'allow_blank': True},
            'gender': {'required': False, 'allow_null': True, 'allow_blank': True},
        }

    def validate_employee_code(self, value):
        code = (value or '').strip().upper()
        if not code:
            raise serializers.ValidationError('Employee code is required.')
        clash = Employee.objects.filter(employee_code=code)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(f'Employee code {code} already exists.')
        return code

    def validate_employee_name(self, value):
        name = ' '.join((value or '').split())
        if not name:
            raise serializers.ValidationError('Employee name is required.')
        return name

    def validate_phone(self, value):
        value = (value or '').strip()
        if value and not _PHONE.match(value):
            raise serializers.ValidationError('Enter a valid phone number.')
        return value or None

    def validate(self, attrs):
        # An empty text field is no value, not an empty string.
        for name in ('email', 'designation', 'gender'):
            if name in attrs and not (attrs[name] or '').strip():
                attrs[name] = None
        return attrs


# ---------------------------------------------------------------------------
# Payment requests: what the pages read. Plain functions rather than
# ModelSerializers: the shape is the pages' contract, and every field in it
# is chosen, not inherited from the model.
# ---------------------------------------------------------------------------

def _user(user):
    if user is None:
        return None
    return {'id': user.pk, 'name': getattr(user, 'name', '') or user.username,
            'username': user.username}


def _iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else (value or None)


def _amount(value):
    return None if value is None else str(value)


def document_data(doc):
    return {
        'id': doc.pk,
        'kind': doc.kind,
        'sap_doc_entry': doc.sap_doc_entry,
        'sap_doc_num': doc.sap_doc_num,
        'vendor_ref': doc.vendor_ref,
        'doc_date': _iso(doc.doc_date),
        'due_date': _iso(doc.due_date),
        'original_amount': _amount(doc.original_amount),
        'paid_amount': _amount(doc.paid_amount),
        'open_amount': _amount(doc.open_amount),
        'mode': doc.mode,
        'percentage': _amount(doc.percentage),
        'amount': _amount(doc.amount),
        'attachment_file': doc.attachment_file,
        'attachment_count': doc.attachment_count,
        'attachment_date': _iso(doc.attachment_date),
        'attachment_check': doc.attachment_check,
    }


def file_data(row):
    return {
        'id': row.pk,
        'name': row.name,
        'size': row.size,
        'purpose': row.purpose,
        'payout_line_id': row.payout_line_id,
        'uploaded_by': _user(row.uploaded_by),
        'uploaded_on': _iso(row.uploaded_on),
    }


def payout_data(payout):
    if payout is None:
        return None
    return {
        'beneficiary_name': payout.beneficiary_name,
        'to_account_number': payout.to_account_number,
        'to_ifsc': payout.to_ifsc,
        'to_account_manual': payout.to_account_manual,
        'updated_by': _user(payout.updated_by),
        'updated_on': _iso(payout.updated_on),
        'lines': [{
            'id': line.pk,
            'method': line.method,
            'amount': _amount(line.amount),
            'from_account': line.from_account,
            'from_account_label': line.from_account_label,
            'cheque_number': line.cheque_number,
            'cheque_bank': line.cheque_bank,
            'cheque_date': _iso(line.cheque_date),
            'cash_notes': line.cash_notes or [],
            'utr': line.utr,
            'utr_proof': line.utr_proof,
            'utr_recorded_by': _user(line.utr_recorded_by),
            'utr_recorded_on': _iso(line.utr_recorded_on),
        } for line in payout.lines.all()],
    }


def voucher_data(voucher):
    return {
        'id': voucher.pk,
        'version': voucher.version,
        'status': voucher.status,
        'sap_doc_entry': voucher.sap_doc_entry,
        'sap_doc_num': voucher.sap_doc_num,
        'error': voucher.error,
        'posted_by': _user(voucher.posted_by),
        'posted_on': _iso(voucher.posted_on),
        'cancelled_by': _user(voucher.cancelled_by),
        'cancelled_on': _iso(voucher.cancelled_on),
    }


def log_data(row):
    return {
        'id': row.pk,
        'action': row.action,
        'label': row.get_action_display(),
        'cycle': row.cycle,
        'stage_name': row.stage_name,
        'actor': _user(row.actor),
        'on_behalf_of': _user(row.on_behalf_of),
        'from_status': row.from_status,
        'to_status': row.to_status,
        'remarks': row.remarks,
        'data': row.data,
        'created_on': _iso(row.created_on),
    }


#: Log rows whose `data` is account detail: kept in the history, emptied for
#: anyone before Payment (they still see THAT it happened, not the account).
_ACCOUNT_LOGS = {'PAYOUT_UPDATED', 'UTR_RECORDED', 'PARTNER_LINKED'}


def _hide_account(row, see_account):
    if not see_account and row['action'] in _ACCOUNT_LOGS:
        row['data'] = None
    if not see_account and row['action'] in ('FILE_ADDED', 'FILE_REMOVED')             and (row['data'] or {}).get('purpose') not in (None, 'SUPPORTING'):
        row['data'] = None
    return row


#: "Not passed" — distinct from None, which means "looked up: they never decided it".
_UNSET = object()


def request_data(advance, *, user, detail=False, my_decision=_UNSET):
    """One request, as both pages read it. `detail` adds history and the route.

    `my_decision` is the viewer's own latest decision log on it, for the desk.
    A list passes it in from ONE query (`flow.my_decisions`) rather than one per
    row; a single request looks it up itself.
    """
    from advance_payment.services import flow as flow_service

    flow = getattr(advance, 'flow', None)
    can = flow_service.abilities(advance, user)
    # The payee's account, its proofs and its change log: Payment and later only.
    see_account = can['see_account']
    try:
        payout = advance.payout
    except Exception:  # noqa: BLE001 — RelatedObjectDoesNotExist: none yet
        payout = None
    out = {
        'id': advance.pk,
        'request_no': advance.request_no,
        'company': advance.company,
        'request_type': advance.request_type,
        'payment_against': advance.payment_against,
        'payment_against_other': advance.payment_against_other,
        'department': {'id': advance.department_id, 'name': advance.department.name},
        'sub_department': ({'id': advance.sub_department_id, 'name': advance.sub_department.name}
                           if advance.sub_department_id else None),
        'partner_code': advance.partner_code,
        'partner_name': advance.partner_name,
        'partner_not_in_sap': advance.partner_not_in_sap,
        'amount': _amount(advance.amount),
        'currency': advance.currency,
        'expected_date': _iso(advance.expected_date),
        'expected_bill_date': _iso(advance.expected_bill_date),
        'return_method': advance.return_method,
        'return_method_other': advance.return_method_other,
        'installments': advance.installments,
        'emi_amount': _amount(advance.emi_amount),
        'expected_from_date': _iso(advance.expected_from_date),
        'expected_to_date': _iso(advance.expected_to_date),
        'payment_date': _iso(advance.payment_date),
        'priority': advance.priority,
        'budget_code': advance.budget_code,
        'budget_name': advance.budget_name,
        'sub_budget_code': advance.sub_budget_code,
        'sub_budget_name': advance.sub_budget_name,
        'remarks': advance.remarks,
        'owner_employee_id': advance.owner_employee_id,
        'owner_label': advance.owner_label,
        'status': advance.status,
        'created_by': _user(advance.created_by),
        'created_on': _iso(advance.created_on),
        'updated_on': _iso(advance.updated_on),
        'documents': [document_data(d) for d in advance.documents.all()],
        'files': [file_data(f) for f in advance.files.all()
                  if see_account or f.purpose == 'SUPPORTING'],
        'payout': payout_data(payout) if see_account else None,
        'flow': None if flow is None else {
            'status': flow.status,
            'workflow': flow.workflow.code,
            'current_stage': flow.current_stage.name if flow.current_stage_id else '',
            'current_role': flow.current_role,
            'current_user': _user(flow.current_user),
            'cycle': flow.cycle,
            'version': flow.version,
            'total_stages': flow.total_stages,
            'awaiting_me': bool(flow_service.is_actor(flow, user)),
        },
        'can': can,
    }
    live = [v for v in advance.vouchers.all() if v.status == 'POSTED' and v.replaced_by_id is None]
    out['voucher'] = voucher_data(live[-1]) if live else None
    # The last word from an approver who returned, sent back or rejected it:
    # what the creator (or Payment) must act on.
    last = (advance.logs.filter(action__in=['RETURNED', 'SENT_BACK', 'REJECTED'])
            .select_related('actor').order_by('-id').first())
    out['last_decision'] = log_data(last) if last else None
    # What THIS viewer did to it — the desk's "Approved by you" / "Rejected by
    # you". Their own act, so it is not the approver status the desk hides.
    if my_decision is _UNSET:
        my_decision = (flow_service.my_decisions(user, [advance.pk]).get(advance.pk)
                       if getattr(user, 'is_authenticated', False) else None)
    out['my_decision'] = log_data(my_decision) if my_decision else None
    if detail:
        out['vouchers'] = [voucher_data(v) for v in advance.vouchers.all()]
        out['logs'] = [_hide_account(log_data(r), see_account)
                       for r in advance.logs.select_related('actor', 'on_behalf_of')]
        out['stages'] = flow_service.stage_plan(advance)
    return out
