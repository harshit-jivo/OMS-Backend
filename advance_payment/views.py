"""Advance Payment — the lookups a request is raised from.

READ-ONLY, AND EVERY ANSWER COMES FROM SAP.
Five GETs, no POST. Nothing here creates an advance yet: this is the set of
things an advance can be raised AGAINST — an open PO, an open invoice, a
vendor, a customer, or an employee's advance account — and every one of them
is SAP's record, read live.

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
import logging

from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.companies import COMPANY_CODES
from core.responses import fail, ok

from advance_payment import permissions as ap_perms
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
