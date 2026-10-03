"""Bills and POs sent to an Advance Payment User or Approver, to raise a payment request from.

    sender (Advance_Payment_Dispatch)  ticks open SAP bills / POs, picks a user
                                        holding `advance_payment_user` or
                                        `advance_payment_approver`
    recipient                           sees them under "Assigned to Me"; raising a
                                        request from one links it (RAISED), or they
                                        dismiss it
    sender                              may withdraw one still OPEN

The document is checked against SAP when sent (it exists, is open, and is the
vendor's), and its facts are copied from SAP, not from the browser.
"""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q

from advance_payment.models import AssignmentStatus, DocumentAssignment, DocumentKind
from advance_payment.permissions import RECIPIENT_ROLES
from core.companies import COMPANY_CODES

KINDS = {'BILL': DocumentKind.BILL, 'PO': DocumentKind.PO}


class AssignmentInvalid(Exception):
    def __init__(self, problems, status=400):
        self.problems = list(problems)
        self.status = status
        super().__init__(' '.join(self.problems))


def recipients():
    """Active users holding an Advance Payment User or Approver role, primary or extra, by name."""
    User = get_user_model()
    role = Q()
    for name in RECIPIENT_ROLES:
        role |= Q(role__name__iexact=name) | Q(extra_roles__name__iexact=name)
    return User.objects.filter(role, is_active=True).distinct().order_by('name', 'username')


def send(*, company, recipient_id, documents, note, user):
    """Send each document to the recipient. Returns the new assignments.

    `documents`: `[{"kind": "BILL"|"PO", "sap_doc_entry": 123}]`. All or
    nothing: one document that is not open in SAP refuses the whole send.
    """
    from advance_payment.services import sap as sap_service

    company = str(company or '').strip().upper()
    problems = []
    if company not in COMPANY_CODES:
        problems.append('Company must be one of: ' + ', '.join(COMPANY_CODES) + '.')
    recipient = recipients().filter(pk=recipient_id).first() if str(recipient_id or '').isdigit() else None
    if recipient is None:
        problems.append('Choose who to send them to: an active Advance Payment User or Approver.')
    wanted = []
    for raw in documents or []:
        kind = KINDS.get(str((raw or {}).get('kind') or '').upper())
        try:
            entry = int((raw or {}).get('sap_doc_entry'))
        except (TypeError, ValueError):
            entry = None
        if kind is None or entry is None:
            problems.append('Each document needs its kind (BILL or PO) and SAP entry.')
            continue
        if (kind, entry) not in wanted:
            wanted.append((kind, entry))
    if not wanted:
        problems.append('Choose at least one bill or PO.')
    if problems:
        raise AssignmentInvalid(problems)

    live = {}
    try:
        for kind in {k for k, _e in wanted}:
            for entry, facts in sap_service.live_documents(company, kind, [e for k, e in wanted if k == kind]).items():
                live[(kind, entry)] = facts
    except sap_service.SapUnavailable as exc:
        raise AssignmentInvalid([f'Could not check the documents with SAP: {exc}'], status=503) from exc
    for kind, entry in wanted:
        facts = live.get((kind, entry))
        label = f'{DocumentKind(kind).label} {facts["doc_num"] if facts else entry}'
        if facts is None:
            problems.append(f'{label} is not in {company}\'s SAP.')
        elif facts['status'] != 'OPEN':
            problems.append(f'{label} is {facts["status"].lower()} in SAP.')
    if problems:
        raise AssignmentInvalid(problems)

    made = []
    try:
        with transaction.atomic():
            for kind, entry in wanted:
                facts = live[(kind, entry)]
                made.append(DocumentAssignment.objects.create(
                    company=company, kind=kind, sap_doc_entry=entry, sap_doc_num=str(facts['doc_num'] or entry),
                    card_code=facts['card_code'], card_name=facts['card_name'], vendor_ref=facts['vendor_ref'],
                    doc_date=facts['doc_date'] or None, due_date=facts['due_date'] or None,
                    doc_total=facts['doc_total'], open_amount=facts['open'],
                    note=str(note or '').strip()[:2000], assigned_to=recipient, assigned_by=user))
    except IntegrityError as exc:
        raise AssignmentInvalid(['One of these is already waiting, open, with that user.'], status=409) from exc
    return made


def _one(pk):
    found = DocumentAssignment.objects.select_related('assigned_to', 'assigned_by', 'request').filter(pk=pk).first()
    if found is None:
        raise AssignmentInvalid(['No such assignment.'], status=404)
    return found


def dismiss(pk, *, user):
    """The recipient sets an OPEN one aside."""
    found = _one(pk)
    if found.assigned_to_id != user.pk:
        raise AssignmentInvalid(['Only the person it was sent to may dismiss it.'], status=403)
    if found.status != AssignmentStatus.OPEN:
        raise AssignmentInvalid(['It is no longer open.'], status=409)
    found.status = AssignmentStatus.DISMISSED
    found.save(update_fields=['status', 'updated_on'])
    return found


def withdraw(pk, *, user):
    """The sender takes back an OPEN one."""
    found = _one(pk)
    if found.assigned_by_id != user.pk:
        raise AssignmentInvalid(['Only the person who sent it may withdraw it.'], status=403)
    if found.status != AssignmentStatus.OPEN:
        raise AssignmentInvalid(['It is no longer open.'], status=409)
    found.status = AssignmentStatus.WITHDRAWN
    found.save(update_fields=['status', 'updated_on'])
    return found


def link_request(pk, advance, *, user):
    """Mark the assignment RAISED by `advance`, if it is the recipient's, open, and the request pays that document.

    Quietly does nothing otherwise: the request stands on its own.
    """
    try:
        found = _one(int(pk))
    except (AssignmentInvalid, TypeError, ValueError):
        return None
    if found.assigned_to_id != user.pk or found.status != AssignmentStatus.OPEN or found.company != advance.company:
        return None
    if not advance.documents.filter(kind=found.kind, sap_doc_entry=found.sap_doc_entry).exists():
        return None
    found.status = AssignmentStatus.RAISED
    found.request = advance
    found.save(update_fields=['status', 'request', 'updated_on'])
    return found
