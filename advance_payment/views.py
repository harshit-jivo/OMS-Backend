"""Advance Payment — the lookups a request is raised from, and the requests.

THE LOOKUPS ARE READ-ONLY, AND EVERY ANSWER COMES FROM SAP: the things an
advance can be raised AGAINST — an open PO, an open invoice, a vendor, a
customer, or an employee's advance account — each SAP's record, read live.
The payment requests themselves, and their approval, are at the end of this
file (`RequestListView` onwards); the rules are in `services/flow.py`.

`?company=` IS REQUIRED ON ALL FIVE, and a missing one is a 400 rather than a
default. The three company databases hold different documents under the same
DocNum, so defaulting to OIL would not fail — it would return another
company's vendors and another company's orders, which is the failure mode
`docs/CODEBASE_AND_REFACTOR_PLAN.md` §1.1 calls the single most important fact
about this system.

Errors are told apart on purpose:

    400  unknown or missing company, bad party_type   — the caller's request
    503  HANA unreachable or the query failed          — not the caller's fault

An empty `results` therefore always means "SAP has none of these", never "we
could not ask". That distinction is what `production`'s `SapUnavailable`
exists for, after a JSAP job reported success for 33 days while writing
nothing.

Responses use the project's `{success, message, data}` envelope.
"""
import hashlib
import json
import logging
from datetime import date
from urllib.parse import quote

from django.core.cache import cache
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.companies import COMPANY_CODES
from core.permissions import IsAdminRole, effective_keys, is_admin
from core.responses import created, fail, ok

from advance_payment import permissions as ap_perms
from advance_payment.models import (
    AdvanceRequest, Department, Employee, EmployeeRole, FilePurpose, LogAction, PayoutLine,
    RequestFile, SapVoucher, SubDepartment)
from advance_payment.serializers import EmployeeSerializer, request_data
from advance_payment.services import (
    attachment_files, invoice_fields, payment_proof, proof_reader)
from advance_payment.services import flow as flow_service
from advance_payment.services import requests as request_service
from advance_payment.services import sap as sap_service

logger = logging.getLogger(__name__)


class _Lookup(APIView):
    """Shared shape: authorise, read `?company=`, call SAP, envelope the rows.

    Subclasses provide `resource` (what the rows are, for the message) and
    `fetch(request, company)`.
    """

    #: For the response message and the log line. Set per subclass.
    resource = 'rows'

    def get_permissions(self):
        return [IsAuthenticated(), ap_perms.CanViewLookups()]

    def _company(self, request):
        """`(company, error_response)`.

        Validated against `COMPANY_CODES` here as well as by
        `_schema_for_branch` further down, because the two answer different
        questions: this one is "is that a company?", that one is "is it
        configured?". A caller who sends OILL should be told which of those
        went wrong.
        """
        raw = (request.query_params.get('company') or '').strip().upper()
        if not raw:
            return None, fail(
                'company is required — one of: ' + ', '.join(COMPANY_CODES),
                status=http_status.HTTP_400_BAD_REQUEST)
        if raw not in COMPANY_CODES:
            return None, fail(
                f'Unknown company {raw!r}. Expected one of: '
                + ', '.join(COMPANY_CODES),
                status=http_status.HTTP_400_BAD_REQUEST)
        return raw, None

    def get(self, request):
        company, error = self._company(request)
        if error:
            return error
        try:
            rows = self.fetch(request, company)
        except ValueError as exc:
            # `sap.UnknownCompany` is a ValueError too: a company that passed
            # the code check but has no schema configured.
            return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
        except sap_service.SapUnavailable as exc:
            return fail(str(exc),
                        status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        return ok({
            'company': company,
            'count': len(rows),
            'results': rows,
        })

    def fetch(self, request, company):  # pragma: no cover - interface
        raise NotImplementedError


def _search(request):
    return request.query_params.get('search')


def _limit(request):
    return request.query_params.get('limit')


class VendorsView(_Lookup):
    """GET /api/advance-payments/vendors/?company=&search=&limit=

    Active, unfrozen suppliers. Frozen partners are excluded because SAP will
    refuse a payment to one — better to keep them out of the picker than to
    discover it after the request has been approved.
    """

    resource = 'vendors'

    def fetch(self, request, company):
        return sap_service.vendors(company, _search(request), _limit(request))


class CustomersView(_Lookup):
    """GET /api/advance-payments/customers/?company=&search=&limit=

    For the other direction — an advance RECEIVED against a customer.
    """

    resource = 'customers'

    def fetch(self, request, company):
        return sap_service.customers(company, _search(request), _limit(request))


class EmployeesView(_Lookup):
    """GET /api/advance-payments/employees/?company=&search=&limit=

    From the chart of accounts, not from OHEM — see
    `services/sap.py::_EMPLOYEE_SQL`. Each row carries the parsed
    `employee_code` and `employee_name` AND the raw `acct_code` / `acct_name`,
    so an account named outside the convention is still usable.
    """

    resource = 'employee advance accounts'

    def fetch(self, request, company):
        return sap_service.employees(company, _search(request), _limit(request))


class OpenPurchaseOrdersView(_Lookup):
    """GET /api/advance-payments/open-purchase-orders/
           ?company=&card_code=&search=&limit=

    `open_amount` is the undelivered portion, not the order total: a PO half
    received is not one to advance the full value against.
    """

    resource = 'open purchase orders'

    def fetch(self, request, company):
        return sap_service.open_purchase_orders(
            company,
            card_code=request.query_params.get('card_code'),
            search=_search(request),
            limit=_limit(request),
        )


class OpenDocumentsView(_Lookup):
    """GET /api/advance-payments/open-documents/?company=&card_code=&limit=

    EVERY open document against one business partner — invoices, credit memos,
    payments on account and journals — with what is still outstanding on each.
    Works for a vendor or a customer; the card code decides which, and
    `party_type` comes back in the response rather than being asked for.

    This is the partner's ledger, not a table scan of OINV/OPCH: it reads the
    journal lines, which is where SAP's own Ageing report reads from and the
    only place that has all seven document types. See
    `services/sap.py::_OPEN_DOC_SQL` — including why it is NOT `OIPF`.

    `card_code` is required. An unknown one is a **404**, not an empty list: a
    mistyped vendor code and a vendor with nothing outstanding must not look
    the same on a screen that is about to raise a payment.
    """

    resource = 'open documents'

    def get(self, request):
        company, error = self._company(request)
        if error:
            return error

        card_code = (request.query_params.get('card_code') or '').strip()
        if not card_code:
            return fail('card_code is required (a vendor or customer code).',
                        status=http_status.HTTP_400_BAD_REQUEST)

        try:
            info = sap_service.partner(company, card_code)
            if not info:
                return fail(
                    f'No business partner {card_code!r} in {company}.',
                    status=http_status.HTTP_404_NOT_FOUND)
            rows, summary = sap_service.open_documents(
                company, card_code, limit=_limit(request),
                bills_only=str(
                    request.query_params.get('bills_only') or ''
                ).strip().lower() in ('1', 'true', 'yes'))
        except ValueError as exc:
            return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
        except sap_service.SapUnavailable as exc:
            return fail(str(exc),
                        status=http_status.HTTP_503_SERVICE_UNAVAILABLE)

        return ok({
            'company': company,
            'partner': info,
            'summary': summary,
            'count': len(rows),
            'results': rows,
        })


class OtherDocumentsView(_Lookup):
    """GET /api/advance-payments/open-other-documents/?company=&card_code=&limit=

    The vendor's "All": every open document EXCEPT its bills and POs — goods
    receipts not yet invoiced, goods returns not yet credited, and the
    non-invoice ledger items (credit memos, payments on account, journals).
    See `services/sap.py::other_documents` for why it takes three sources.

    Same row shape as `/open-documents/`. Unknown `card_code` is a 404.
    """

    resource = 'other open documents'

    def get(self, request):
        company, error = self._company(request)
        if error:
            return error
        card_code = (request.query_params.get('card_code') or '').strip()
        if not card_code:
            return fail('card_code is required (a vendor code).',
                        status=http_status.HTTP_400_BAD_REQUEST)
        try:
            info = sap_service.partner(company, card_code)
            if not info:
                return fail(f'No business partner {card_code!r} in {company}.',
                            status=http_status.HTTP_404_NOT_FOUND)
            rows, summary = sap_service.other_documents(
                company, card_code, limit=_limit(request))
        except ValueError as exc:
            return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
        except sap_service.SapUnavailable as exc:
            return fail(str(exc),
                        status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        return ok({
            'company': company,
            'partner': info,
            'summary': summary,
            'count': len(rows),
            'results': rows,
        })


class PartnerBankAccountsView(_Lookup):
    """GET /api/advance-payments/partner-bank-accounts/?company=&card_code=

    The payee's bank accounts as SAP holds them, for the approver's To Account
    Number and IFSC. The DEFAULT comes first and is flagged, so the form can
    pre-fill it and still offer the others.

    Unknown `card_code` is a 404. A partner with no account on file is an empty
    list, and the approver types the details in themselves.
    """

    resource = 'partner bank accounts'

    def get(self, request):
        company, error = self._company(request)
        if error:
            return error
        card_code = (request.query_params.get('card_code') or '').strip()
        if not card_code:
            return fail('card_code is required (a vendor or imprest code).',
                        status=http_status.HTTP_400_BAD_REQUEST)
        try:
            if not sap_service.partner(company, card_code):
                return fail(f'No business partner {card_code!r} in {company}.',
                            status=http_status.HTTP_404_NOT_FOUND)
            rows, default_number = sap_service.partner_bank_accounts(company, card_code)
        except ValueError as exc:
            return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
        except sap_service.SapUnavailable as exc:
            return fail(str(exc),
                        status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        return ok({
            'company': company,
            'card_code': card_code,
            'default_account_number': default_number,
            'count': len(rows),
            'results': rows,
        })


class DocumentAttachmentReadView(_Lookup):
    """GET /api/advance-payments/document-attachment/read/?company=&kind=po|bill&doc_entry=

    Read a PO's / bill's latest SAP attachment and pull out its invoice
    number, date, amount, party name, bank account number and IFSC, each
    checked against what SAP holds for that document (`services/
    invoice_fields.py`): `{value, sap, match}` per field.

    The file is read as `payment-proof/` reads one: a PDF's own text directly,
    a photo or a scanned page by OCR. The text is cached for a day per
    attachment, so ticking the same bill again does not OCR it again; the SAP
    side is read fresh each time.

    404 no attachment / no such document; 503 SAP, the file service or OCR
    unavailable; 400 a file that cannot be read.
    """

    resource = 'document attachment'
    CACHE_SECONDS = 24 * 60 * 60

    def get(self, request):
        company, error = self._company(request)
        if error:
            return error
        kind = request.query_params.get('kind')
        doc_entry = request.query_params.get('doc_entry')
        try:
            facts = sap_service.document_facts(company, kind, doc_entry)
            meta = sap_service.document_attachment(company, kind, doc_entry)
        except ValueError as exc:
            return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
        except sap_service.SapUnavailable as exc:
            return fail(str(exc), status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        if not facts:
            return fail('No such document in SAP.', status=http_status.HTTP_404_NOT_FOUND)
        if not meta:
            return fail('This document has no attachment in SAP.',
                        status=http_status.HTTP_404_NOT_FOUND)

        name = meta['file_name']
        # Hashed: file names carry spaces and punctuation no cache key may.
        key = 'ap:attachment-read:' + hashlib.sha1(
            f'{company}|{kind}|{doc_entry}|{name}'.encode('utf-8')).hexdigest()
        document = cache.get(key)
        if document is None:
            try:
                content, _content_type = attachment_files.fetch(company, name)
                document = proof_reader.read(content, name)
            except attachment_files.AttachmentNotFound as exc:
                return fail(str(exc), status=http_status.HTTP_404_NOT_FOUND)
            except (attachment_files.AttachmentNotConfigured,
                    attachment_files.AttachmentUnavailable,
                    proof_reader.OcrUnavailable) as exc:
                return fail(str(exc), status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
            except proof_reader.ProofUnreadable as exc:
                return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
            cache.set(key, document, self.CACHE_SECONDS)

        # The vendor's bank accounts in SAP: what the account on the invoice
        # is checked against. Not having them is not a reason to fail.
        accounts, ifscs = [], []
        if facts['card_code']:
            try:
                bank_rows, _default = sap_service.partner_bank_accounts(company, facts['card_code'])
                for account in bank_rows:
                    if account.get('account_number'):
                        accounts.append(account['account_number'])
                    if account.get('ifsc'):
                        ifscs.append(account['ifsc'])
            except sap_service.SapUnavailable as exc:
                logger.warning('advance_payment: bank accounts unreadable for %s: %s',
                               facts['card_code'], exc)

        fields = invoice_fields.extract(document['rows'], {
            'invoice_number': facts['vendor_ref'] or None,
            'invoice_date': date.fromisoformat(facts['document_date']) if facts['document_date'] else None,
            'totals': [float(facts['gross_total']), float(facts['doc_total'])],
            'party_name': facts['card_name'] or None,
            'accounts': accounts,
            'ifscs': ifscs,
        })
        return ok({
            'file_name': name,
            'attachment_count': meta['count'],
            'source': document['source'],
            'pages': document['pages'],
            'fields': fields,
            'sap': facts,
        })


class EmployeeMasterView(APIView):
    """GET / POST /api/advance-payments/employee-master/   (administrators only)

    The employee master (`models.Employee`) for the Add Employee page.

    GET   the employees not deleted, by name. Filters: `?search=` (code or
          name), `?role=1|2|3`, `?is_active=true|false`.
    POST  add one: `employee_code`, `employee_name` and `role` required;
          `employee_id` (JSAP's), `email`, `phone`, `designation`, `gender`,
          `is_active` optional. `created_on` is now.

    Not the `employees/` lookup below, which lists SAP's employee-ADVANCE
    G/L accounts, a different thing.
    """

    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        rows = Employee.objects.alive()
        search = (request.query_params.get('search') or '').strip()
        if search:
            rows = rows.filter(Q(employee_code__icontains=search) | Q(employee_name__icontains=search))
        role = (request.query_params.get('role') or '').strip()
        if role:
            if role not in {str(r) for r in EmployeeRole.values}:
                return fail('role must be 1, 2 or 3.', status=http_status.HTTP_400_BAD_REQUEST)
            rows = rows.filter(role=int(role))
        active = (request.query_params.get('is_active') or '').strip().lower()
        if active in ('true', 'false'):
            rows = rows.filter(is_active=active == 'true')
        data = EmployeeSerializer(rows, many=True).data
        return ok({'count': len(data), 'results': data})

    def post(self, request):
        serializer = EmployeeSerializer(data=request.data)
        if not serializer.is_valid():
            first = next(iter(serializer.errors.values()))
            message = first[0] if isinstance(first, list) and first else 'Check the employee details.'
            return fail(str(message), errors=serializer.errors,
                        status=http_status.HTTP_400_BAD_REQUEST)
        employee = serializer.save()
        logger.info('advance_payment: employee %s added by %s',
                    employee.employee_code, getattr(request.user, 'pk', None))
        return created(EmployeeSerializer(employee).data, 'Employee added.')


class DepartmentsView(APIView):
    """GET /api/advance-payments/departments/

    OMS's department list (copied from JSAP, now OMS's own: migration 0004)
    with each department's sub-departments, active ones only, by name:

        [{"id": 35, "name": "Finance",
          "sub_departments": [{"id": 92, "name": "AP"}, ...]}, ...]

    For the request form's Department / Sub-department pickers. The ids are
    what the Workflow Engine's queries match on.
    """

    def get_permissions(self):
        return [IsAuthenticated(), ap_perms.CanViewLookups()]

    def get(self, request):
        subs = {}
        for sub in SubDepartment.objects.filter(is_active=True).order_by('name'):
            subs.setdefault(sub.department_id, []).append({'id': sub.id, 'name': sub.name})
        results = [
            {'id': d.id, 'name': d.name, 'sub_departments': subs.get(d.id, [])}
            for d in Department.objects.filter(is_active=True).order_by('name')
        ]
        return ok({'count': len(results), 'results': results})


class EmployeeDirectoryView(_Lookup):
    """GET /api/advance-payments/employee-directory/

    The employee master (`models.Employee`) as the REQUEST form reads it:
    active employees only, and only what a picker needs. Same key as the other
    lookups (`Advance_Payment`), unlike `employee-master/`, which is admins'.

    ?roles=1,2        only those roles (1 HOD, 2 Sub-HOD, 3 Executive): the
                      Ownership picker asks for HODs and Sub-HODs
    ?search=          code or name
    ?not_in_sap=1     only employees with NO employee-advance account in SAP
                      for `?company=` (required then): the Employee picker
                      offers them marked "Not in SAP"
    """

    resource = 'employees'

    def get(self, request):
        not_in_sap = (request.query_params.get('not_in_sap') or '').strip().lower() in ('1', 'true')
        company = None
        if not_in_sap:
            company, error = self._company(request)
            if error:
                return error

        rows = Employee.objects.active()
        roles = [r.strip() for r in (request.query_params.get('roles') or '').split(',') if r.strip()]
        if roles:
            if any(r not in {str(v) for v in EmployeeRole.values} for r in roles):
                return fail('roles must be a comma-separated list of 1, 2, 3.',
                            status=http_status.HTTP_400_BAD_REQUEST)
            rows = rows.filter(role__in=[int(r) for r in roles])
        search = (request.query_params.get('search') or '').strip()
        if search:
            rows = rows.filter(Q(employee_code__icontains=search) | Q(employee_name__icontains=search))

        if not_in_sap:
            try:
                in_sap = sap_service.employee_advance_codes(company)
            except ValueError as exc:
                return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
            except sap_service.SapUnavailable as exc:
                return fail(str(exc), status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
            rows = [e for e in rows if e.employee_code.upper() not in in_sap]

        results = [{
            'employee_code': e.employee_code,
            'employee_name': e.employee_name,
            'role': e.role,
            'role_label': e.get_role_display(),
            'designation': e.designation,
        } for e in rows]
        return ok({'company': company, 'count': len(results), 'results': results})


class DocumentAttachmentView(_Lookup):
    """GET /api/advance-payments/document-attachment/?company=&kind=po|bill&doc_entry=

    The document's LATEST SAP attachment (the last ATC1 line), as the file
    itself, inline: a PDF or image the browser can show. The open PO and
    open invoice lists already say, per row, whether there is one
    (`attachment`), so the screen only asks for a file that exists.

    The file name is read from SAP here, by document, never taken from the
    caller. The file comes from the attachment file service
    (`services/attachment_files.py`), unzipped.

    404 when the document has no attachment, or SAP names a file the share
    does not have; 503 when SAP or the file service cannot be read.
    """

    resource = 'document attachment'

    def get(self, request):
        company, error = self._company(request)
        if error:
            return error
        kind = request.query_params.get('kind')
        doc_entry = request.query_params.get('doc_entry')
        try:
            meta = sap_service.document_attachment(company, kind, doc_entry)
        except ValueError as exc:
            return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
        except sap_service.SapUnavailable as exc:
            return fail(str(exc), status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        if not meta:
            return fail('This document has no attachment in SAP.',
                        status=http_status.HTTP_404_NOT_FOUND)

        name = meta['file_name']
        try:
            content, content_type = attachment_files.fetch(company, name)
        except attachment_files.AttachmentNotFound as exc:
            return fail(str(exc), status=http_status.HTTP_404_NOT_FOUND)
        except attachment_files.AttachmentNotConfigured as exc:
            logger.error('advance_payment: %s', exc)
            return fail(str(exc), status=http_status.HTTP_503_SERVICE_UNAVAILABLE)
        except attachment_files.AttachmentUnavailable as exc:
            return fail(str(exc), status=http_status.HTTP_503_SERVICE_UNAVAILABLE)

        response = HttpResponse(content, content_type=content_type)
        response['Content-Disposition'] = f"inline; filename*=UTF-8''{quote(name)}"
        response['X-Attachment-Name'] = quote(name)
        response['X-Attachment-Count'] = str(meta['count'])
        response['Access-Control-Expose-Headers'] = 'X-Attachment-Name, X-Attachment-Count, Content-Disposition'
        return response


class PaymentProofView(_Lookup):
    """POST /api/advance-payments/payment-proof/   (multipart)

    Read a payment's proof (a bank statement, or one payment's advice or
    screenshot; PDF, Excel / CSV, or a photo) and find its UTR.

    Fields:
      file        the proof (required)
      company     OIL | MART | BEVERAGES (required)
      amount      what this payment line paid: the reference is matched to it
      to_account  the account it was paid TO
      card_code   the payee in SAP, whose other accounts are then known too
      invoices    the invoice / bill numbers paid (repeat the field, or comma-separate)

    Returns the best reference and its checks (amount / account / invoice),
    the runners-up, and how the file was read (`source`: pdf-text | ocr |
    pdf-text+ocr | excel | csv). Nothing is stored: the screen decides whether
    to take the UTR. See `services/payment_proof.py`.

    400 unreadable file or bad input; 503 when the file needs OCR and the OCR
    service is not available.
    """

    resource = 'payment proof'

    def get(self, request):  # pragma: no cover - POST only
        return fail('POST the proof as multipart/form-data.',
                    status=http_status.HTTP_405_METHOD_NOT_ALLOWED)

    def post(self, request):
        raw = (request.data.get('company') or '').strip().upper()
        if raw not in COMPANY_CODES:
            return fail('company is required, one of: ' + ', '.join(COMPANY_CODES),
                        status=http_status.HTTP_400_BAD_REQUEST)
        company = raw
        upload = request.FILES.get('file')
        if not upload:
            return fail('Attach the proof as `file`.', status=http_status.HTTP_400_BAD_REQUEST)

        amount = (request.data.get('amount') or '').strip() or None
        if amount is not None:
            try:
                float(amount)
            except ValueError:
                return fail('amount must be a number.', status=http_status.HTTP_400_BAD_REQUEST)
        to_account = (request.data.get('to_account') or '').strip()
        card_code = (request.data.get('card_code') or '').strip()
        invoices = [part.strip()
                    for value in request.data.getlist('invoices')
                    for part in str(value).split(',') if part.strip()]

        # The payee's other SAP accounts, and ours: context, not required. SAP
        # being down must not stop a proof being read.
        other_accounts, own_accounts = [], []
        try:
            if card_code:
                bank_rows, _default = sap_service.partner_bank_accounts(company, card_code)
                other_accounts = [a['account_number'] for a in bank_rows if a.get('account_number')]
            own_accounts = [b['account_number'] for b in sap_service.house_banks(company)
                            if b.get('account_number')]
        except (sap_service.SapUnavailable, ValueError) as exc:
            logger.warning('advance_payment: proof context unavailable for %s: %s', company, exc)

        try:
            document = proof_reader.read(upload.read(), upload.name)
        except proof_reader.ProofUnreadable as exc:
            return fail(str(exc), status=http_status.HTTP_400_BAD_REQUEST)
        except proof_reader.OcrUnavailable as exc:
            return fail(str(exc), status=http_status.HTTP_503_SERVICE_UNAVAILABLE)

        result = payment_proof.extract(
            document['rows'],
            amount=amount,
            accounts=[to_account] if to_account else [],
            other_accounts=other_accounts,
            own_accounts=own_accounts,
            invoices=invoices,
        )
        return ok({
            **result,
            'file_name': upload.name,
            'source': document['source'],
            'pages': document['pages'],
            'ocr_pages': document['ocr_pages'],
            'rows_read': len(document['rows']),
        })


class HouseBanksView(_Lookup):
    """GET /api/advance-payments/house-banks/?company=

    Every bank account the company can pay out of: one row per bank G/L
    (under 1104100 BANK ACCOUNTS and 2201100 BANK CASH CREDIT LOAN), with its
    house-bank details where SAP has them. The house-bank table alone misses
    most of them. See `services/sap.py::_COMPANY_BANKS_SQL`.
    """

    resource = 'bank accounts'

    def fetch(self, request, company):
        return sap_service.house_banks(company)


class CashAccountsView(_Lookup):
    """GET /api/advance-payments/cash-accounts/?company=

    The company's cash G/L accounts: the postable children of `1105000 CASH IN
    HAND`, read from the chart of accounts.
    """

    resource = 'cash accounts'

    def fetch(self, request, company):
        return sap_service.cash_accounts(company)


class BudgetsView(_Lookup):
    """GET /api/advance-payments/budgets/?company=

    The Payment Purpose pickers: the company's active Budget (cost-centre
    dimension 3) and Sub Budget (dimension 4) codes, each row with its `kind`.
    """

    resource = 'budgets'

    def fetch(self, request, company):
        return sap_service.budgets(company)


class OpenInvoicesView(_Lookup):
    """GET /api/advance-payments/open-invoices/
           ?company=&party_type=vendor|customer&card_code=&search=&limit=

    `party_type` picks the ledger: vendor reads OPCH (A/P), customer reads
    OINV (A/R). It defaults to `vendor` — the common case — but an
    unrecognised value is a 400 rather than a silent fallback, because reading
    the wrong ledger would put a customer's invoice in front of someone about
    to pay a supplier.
    """

    resource = 'open invoices'

    def fetch(self, request, company):
        return sap_service.open_invoices(
            company,
            party_type=request.query_params.get('party_type') or 'vendor',
            card_code=request.query_params.get('card_code'),
            search=_search(request),
            limit=_limit(request),
        )


# ---------------------------------------------------------------------------
# Payment requests
# ---------------------------------------------------------------------------
#
#   GET  /requests/?scope=mine|desk          the requester's, or the desk's
#   POST /requests/                          raise (multipart: data=JSON, files)
#   GET  /requests/<id>/                     one, with its history and route
#   POST /requests/<id>/edit/                the creator's edit (multipart, as create,
#                                            plus remove_file_ids, resubmit, version)
#   POST /requests/<id>/<action>/            cancel | resubmit | approve | reject |
#                                            return | send-back   {remarks, version}
#   PUT  /requests/<id>/payout/              the Payment stage's details
#   POST /requests/<id>/payout-lines/<l>/utr/   {utr, proof}
#   POST /requests/<id>/files/               multipart: file, purpose, payout_line_id
#   GET  /requests/<id>/files/<f>/           download
#   DELETE /requests/<id>/files/<f>/         a payout file, by its uploader
#
# WHO: raising and reading your own needs `Advance_Payment`; the desk needs
# `Advance_Payment_Approval`, and ACTING needs, on top, being the current
# stage's user today (`flow.approve` and friends check it on the server).

APPROVAL_KEY = 'Advance_Payment_Approval'

#: At most this many requests in one list; newest first.
LIST_LIMIT = 300


def _is_desk(user):
    return APPROVAL_KEY in effective_keys(user)


def _requests():
    return (AdvanceRequest.objects
            .select_related('department', 'sub_department', 'created_by', 'flow__workflow',
                            'flow__current_stage', 'flow__current_user', 'payout__updated_by')
            .prefetch_related(
                'documents',
                Prefetch('files', queryset=RequestFile.objects.select_related('uploaded_by')),
                Prefetch('payout__lines', queryset=PayoutLine.objects.select_related('utr_recorded_by')),
                Prefetch('vouchers', queryset=SapVoucher.objects.select_related('posted_by', 'cancelled_by')),
            ))


def _flow_error(exc):
    return fail(str(exc), errors={'problems': exc.problems} if exc.problems else None,
                status=exc.status)


def _payload(request):
    """The request's JSON: `data` in a multipart body, or the JSON body itself."""
    raw = request.data.get('data') if hasattr(request.data, 'get') else None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else request.data


class _RequestView(APIView):
    """Either key opens the view; which requests you may see is per object."""

    def get_permissions(self):
        return [IsAuthenticated()]

    def _may_read(self, user, advance):
        """Your own; or, with the desk key, what is or was yours to act on. Admins: any."""
        if advance.created_by_id == user.pk or is_admin(user):
            return True
        return (APPROVAL_KEY in effective_keys(user)
                and advance.pk in flow_service.readable_request_ids(user))

    def _get(self, request, pk):
        advance = _requests().filter(pk=pk).first()
        if advance is None or not self._may_read(request.user, advance):
            raise Http404
        return advance

    def _answer(self, request, advance, message='', status=http_status.HTTP_200_OK):
        fresh = _requests().get(pk=advance.pk)
        return ok(request_data(fresh, user=request.user, detail=True), message, status=status)


class RequestListView(_RequestView):
    def get(self, request):
        scope = (request.query_params.get('scope') or 'mine').lower()
        keys = effective_keys(request.user)
        qs = _requests()
        if scope == 'desk':
            if APPROVAL_KEY not in keys:
                return fail('You do not have the Payments Approval permission.',
                            status=http_status.HTTP_403_FORBIDDEN)
            # Only what is theirs to act on; administrators see every request.
            if not is_admin(request.user):
                qs = qs.filter(pk__in=flow_service.desk_request_ids(request.user))
        else:
            if ap_perms.VIEW_KEY not in keys:
                return fail('You do not have the Payments permission.',
                            status=http_status.HTTP_403_FORBIDDEN)
            qs = qs.filter(created_by=request.user)
        rows = [request_data(r, user=request.user) for r in qs.order_by('-created_on')[:LIST_LIMIT]]
        return ok({'count': len(rows), 'results': rows})

    def post(self, request):
        if ap_perms.VIEW_KEY not in effective_keys(request.user):
            return fail('You do not have the Payments permission.',
                        status=http_status.HTTP_403_FORBIDDEN)
        data = _payload(request)
        if not isinstance(data, dict):
            return fail('Send the request as JSON in `data`.')
        try:
            cleaned = request_service.clean(data)
            advance = flow_service.raise_request(
                cleaned, user=request.user, files=request.FILES.getlist('files'))
        except request_service.RequestInvalid as exc:
            return fail(str(exc), errors={'problems': exc.problems})
        except flow_service.FlowError as exc:
            return _flow_error(exc)
        return self._answer(request, advance, f'{advance.request_no} raised.',
                            status=http_status.HTTP_201_CREATED)


class RequestDetailView(_RequestView):
    def get(self, request, pk):
        return ok(request_data(self._get(request, pk), user=request.user, detail=True))


class RequestEditView(_RequestView):
    def post(self, request, pk):
        advance = self._get(request, pk)
        data = _payload(request)
        if not isinstance(data, dict):
            return fail('Send the request as JSON in `data`.')
        try:
            remove = json.loads(request.data.get('remove_file_ids') or '[]')
        except (TypeError, ValueError):
            remove = []
        resubmit = str(request.data.get('resubmit') or '').lower() in ('1', 'true', 'yes')
        try:
            cleaned = request_service.clean(data)
            flow_service.edit(advance, cleaned, user=request.user,
                              files=request.FILES.getlist('files'), remove_file_ids=remove,
                              resubmit=resubmit, version=request.data.get('version'))
        except request_service.RequestInvalid as exc:
            return fail(str(exc), errors={'problems': exc.problems})
        except flow_service.FlowError as exc:
            return _flow_error(exc)
        return self._answer(request, advance, 'Resubmitted.' if resubmit else 'Saved.')


#: `/requests/<id>/<action>/` -> (service, needs the desk key, done message)
_ACTIONS = {
    'cancel': (flow_service.cancel, False, 'Cancelled.'),
    'resubmit': (flow_service.resubmit, False, 'Resubmitted.'),
    'reject': (flow_service.reject, True, 'Rejected.'),
    'return': (flow_service.return_to_creator, True, 'Returned to its creator.'),
    'send-back': (flow_service.send_back, True, 'Sent back to Payment.'),
}


class RequestActionView(_RequestView):
    def post(self, request, pk, action):
        advance = self._get(request, pk)
        remarks = (request.data.get('remarks') or '').strip()
        version = request.data.get('version')
        if action == 'approve':
            if not _is_desk(request.user):
                return fail('You do not have the Payments Approval permission.',
                            status=http_status.HTTP_403_FORBIDDEN)
            try:
                advance, error = flow_service.approve(
                    advance, user=request.user, remarks=remarks, version=version)
            except flow_service.FlowError as exc:
                return _flow_error(exc)
            if error:
                return fail(f'Not approved: {error}', status=http_status.HTTP_502_BAD_GATEWAY)
            return self._answer(request, advance, 'Approved.')
        if action not in _ACTIONS:
            raise Http404
        service, desk_only, done = _ACTIONS[action]
        if desk_only and not _is_desk(request.user):
            return fail('You do not have the Payments Approval permission.',
                        status=http_status.HTTP_403_FORBIDDEN)
        try:
            advance = service(advance, user=request.user, remarks=remarks, version=version)
        except flow_service.FlowError as exc:
            return _flow_error(exc)
        return self._answer(request, advance, done)


class RequestPayoutView(_RequestView):
    def put(self, request, pk):
        advance = self._get(request, pk)
        if not _is_desk(request.user):
            return fail('You do not have the Payments Approval permission.',
                        status=http_status.HTTP_403_FORBIDDEN)
        try:
            flow_service.save_payout(advance, request.data, user=request.user,
                                     version=request.data.get('version'))
        except flow_service.FlowError as exc:
            return _flow_error(exc)
        return self._answer(request, advance, 'Payment details saved.')


class RequestConfirmPasswordView(_RequestView):
    """POST /requests/<id>/confirm-password/ {password} -> {token}

    The Payment user's password, before typing the payee's bank account by
    hand. The token goes back with the payout as `manual_token`.
    """

    def post(self, request, pk):
        advance = self._get(request, pk)
        try:
            token = flow_service.confirm_password(
                advance, user=request.user, password=request.data.get('password') or '')
        except flow_service.FlowError as exc:
            return _flow_error(exc)
        return ok({'token': token, 'expires_in': flow_service.MANUAL_TOKEN_SECONDS},
                  'Password confirmed.')


class RequestUtrView(_RequestView):
    def post(self, request, pk, line_id):
        advance = self._get(request, pk)
        try:
            flow_service.record_utr(advance, line_id, user=request.user,
                                    utr=request.data.get('utr'), proof=request.data.get('proof'))
        except flow_service.FlowError as exc:
            return _flow_error(exc)
        return self._answer(request, advance, 'UTR recorded.')


#: Files the approval desk adds; supporting files travel with the request itself.
_DESK_PURPOSES = (FilePurpose.BANK_PROOF, FilePurpose.PAYMENT_PROOF)


class RequestFilesView(_RequestView):
    def post(self, request, pk):
        advance = self._get(request, pk)
        purpose = (request.data.get('purpose') or '').upper()
        upload = request.FILES.get('file')
        if purpose not in _DESK_PURPOSES or upload is None:
            return fail('Send one `file`, with purpose BANK_PROOF or PAYMENT_PROOF.')
        if not flow_service.may_add_file(getattr(advance, 'flow', None), request.user, purpose):
            return fail('Files are added by the Payment stage, or after completion by whoever '
                        'records the payment.', status=http_status.HTTP_403_FORBIDDEN)
        line = None
        line_id = request.data.get('payout_line_id')
        if line_id:
            line = PayoutLine.objects.filter(pk=line_id, payout__request=advance).first()
            if line is None:
                return fail('No such payment line.')
        try:
            request_service.check_files([upload])
        except request_service.RequestInvalid as exc:
            return fail(str(exc))
        (row,) = request_service.add_files(advance, [upload], user=request.user,
                                           purpose=purpose, payout_line=line)
        flow_service.log(advance, LogAction.FILE_ADDED, user=request.user,
                         data={'file': row.name, 'purpose': purpose, 'line': line_id or None})
        return self._answer(request, advance, f'{row.name} added.')


class RequestFileView(_RequestView):
    def get(self, request, pk, file_id):
        advance = self._get(request, pk)
        row = advance.files.filter(pk=file_id).first()
        if row is None:
            raise Http404
        response = FileResponse(row.file.open('rb'), as_attachment=False, filename=row.name)
        return response

    def delete(self, request, pk, file_id):
        advance = self._get(request, pk)
        row = advance.files.filter(pk=file_id, purpose__in=_DESK_PURPOSES).first()
        if row is None:
            raise Http404
        flow = getattr(advance, 'flow', None)
        if row.uploaded_by_id != request.user.pk or not flow_service.may_add_file(
                flow, request.user, row.purpose):
            return fail('Only its uploader may remove it, while they still may add files.',
                        status=http_status.HTTP_403_FORBIDDEN)
        name = row.name
        row.file.delete(save=False)
        row.delete()
        flow_service.log(advance, LogAction.FILE_REMOVED, user=request.user,
                         data={'file': name, 'purpose': row.purpose})
        return self._answer(request, advance, f'{name} removed.')
