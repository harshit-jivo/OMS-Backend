"""SAP reads and the one SAP write this module makes.

READ: the customer card — OCRD, one partner, bound parameters.

WRITE: `PATCH /BusinessPartners('<code>')` with `{"CreditLimit": <new>}`.
That is what JSAP did: live OCRD history shows each approved request landing as
a `CreditLine` change by the Service Layer user, and no HANA procedure exists
for it. There is no other write and no expiry job — `valid_till` is recorded on
the request only (see `models.CreditLimitRequest.valid_till`).
"""
import json
import logging
from decimal import Decimal
from urllib.parse import quote

from django.core.exceptions import ValidationError
from django.utils import timezone

from hana.services.connection import (
    HANAConnection, HanaSchemaError, Queries, column_exists,
)

logger = logging.getLogger(__name__)


class SapUnavailable(Exception):
    """SAP could not be read. The message is safe to show."""


class SapWriteError(Exception):
    """SAP refused or did not answer the write. Carries the evidence, which the
    caller stores AFTER its transaction has rolled back (`record_failure`)."""

    def __init__(self, message, *, payload=None, response=''):
        super().__init__(message)
        self.payload = payload
        self.response = response


def _schema(company):
    try:
        return Queries._schema_for_branch(company)
    except HanaSchemaError as exc:
        raise SapUnavailable(str(exc)) from exc


def _money(value):
    return Decimal(str(value)) if value is not None else Decimal('0')


def customer(company, card_code):
    """`{card_code, card_name, card_type, main_group, balance, credit_limit}`
    for one customer, or None when SAP has no such partner."""
    code = (card_code or '').strip()
    if not code:
        return None
    schema = _schema(company)
    try:
        with HANAConnection() as conn:
            # A user-defined field is per company database; ask, don't assume.
            group = ('T0."U_Main_Group"'
                     if column_exists(conn, schema, 'OCRD', 'U_Main_Group')
                     else "''")
            rows = conn.execute(
                f'SELECT T0."CardCode", T0."CardName", T0."CardType", '
                f'{group} AS "MainGroup", T0."Balance", T0."CreditLine" '
                f'FROM "{schema}"."OCRD" T0 WHERE T0."CardCode" = ?',
                [code])
    except Exception as exc:  # noqa: BLE001 — hdbcli raises a broad family
        logger.warning('CL: reading customer %s/%s failed: %s',
                       company, code, exc, exc_info=True)
        raise SapUnavailable(
            f'Could not read customer {code} from SAP ({company}).') from exc
    if not rows:
        return None
    r = rows[0]
    return {
        'card_code': (r['CardCode'] or '').strip(),
        'card_name': (r['CardName'] or '').strip(),
        'card_type': (r['CardType'] or '').strip(),
        'main_group': (r['MainGroup'] or '').strip(),
        'balance': _money(r['Balance']),
        'credit_limit': _money(r['CreditLine']),
    }


def _commitment_limit(company, card_code):
    """The customer's current `OCRD.DebtLine`, read live at approval time."""
    schema = _schema(company)
    try:
        with HANAConnection() as conn:
            rows = conn.execute(
                f'SELECT "DebtLine" FROM "{schema}"."OCRD" WHERE "CardCode" = ?',
                [card_code])
    except Exception as exc:  # noqa: BLE001 — hdbcli raises a broad family
        logger.warning('CL: reading commitment limit for %s/%s failed: %s',
                       company, card_code, exc, exc_info=True)
        raise SapUnavailable(
            f'Could not read customer {card_code} from SAP ({company}).') from exc
    if not rows:
        raise SapUnavailable(f'SAP has no customer {card_code} in {company}.')
    return _money(rows[0]['DebtLine'])


def apply_limit(flow):
    """Write the approved limit to SAP. Raises `SapWriteError` on refusal.

    Called inside the final approval's transaction, BEFORE it writes
    APPROVED: a refusal rolls the approval back, so "approved" always means
    the limit is in SAP. On success the evidence is saved on the caller's
    connection and commits with the approval.
    """
    from payments import sap_client, sap_company

    req = flow.request
    # OData key literal: quotes doubled, then URL-encoded. The code came from
    # SAP itself at submission, but a path is no place to trust that.
    key = quote(req.card_code.replace("'", "''"), safe='')
    path = f"/BusinessPartners('{key}')"
    payload = {'company': req.company, 'method': 'PATCH', 'path': path}

    try:
        # SAP refuses a credit limit above the commitment limit (OCRD.DebtLine,
        # error -5002) — verified on the TEST company, including for customers
        # already above it. So the commitment limit is raised to the new
        # credit limit when it is lower, and never lowered.
        commitment = _commitment_limit(req.company, req.card_code)
        body = {'CreditLimit': str(req.new_credit_limit),
                'MaxCommitment': str(max(commitment, req.new_credit_limit))}
        payload['body'] = body
        company_db = sap_company.resolve_company_db(req.company)
        sap_client.request('PATCH', path, company_db=company_db,
                           json_body=body)
    except SapUnavailable as exc:
        raise SapWriteError(f'{exc} The request has NOT been approved.',
                            payload=payload, response=str(exc)) from exc
    except ValidationError as exc:
        raise SapWriteError(
            f'SAP is not configured for {req.company}: {exc.messages[0]}',
            payload=payload, response=str(exc)) from exc
    except sap_client.SapError as exc:
        logger.warning('CL: SAP refused limit for request %s: %s',
                       req.pk, exc)
        detail = json.dumps(exc.payload, default=str) if exc.payload else str(exc)
        if exc.status_code is None:
            message = (f'SAP did not answer ({exc}). The limit may or may not '
                       f'have been set — check the customer in SAP before '
                       f'approving again.')
        else:
            message = (f'SAP refused the new credit limit: {exc}. The request '
                       f'has NOT been approved.')
        raise SapWriteError(message, payload=payload, response=detail) from exc

    flow.sap_payload = payload
    flow.sap_response = (f'CreditLimit set to {req.new_credit_limit} for '
                         f'{req.card_code} in {req.company} at '
                         f'{timezone.now().isoformat()}.')
    flow.save(update_fields=['sap_payload', 'sap_response', 'updated_at'])


def record_failure(flow, error):
    """Store a refused write after the approval rolled back. Never raises —
    recording must not mask the SAP error the caller is reporting."""
    from credit_limit.models import CreditLimitFlow

    try:
        CreditLimitFlow.objects.filter(pk=flow.pk).update(
            sap_payload=getattr(error, 'payload', None),
            sap_response=getattr(error, 'response', '') or str(error),
            updated_at=timezone.now())
    except Exception:  # noqa: BLE001
        logger.warning('CL: could not record SAP failure for flow %s',
                       flow.pk, exc_info=True)
