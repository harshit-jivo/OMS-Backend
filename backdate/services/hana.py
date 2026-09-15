"""Writing an approved BackDate grant into SAP (HANA `OPEN_BKDT`).

OWNERSHIP
---------
The Workflow Engine never calls HANA. It says which workflow applied and who
approved; THIS module decides that a completed approval means SAP should be
written to, builds the payload, and records what happened.

THE PROCEDURE, AS VERIFIED AGAINST THE LIVE CATALOGUE
-----------------------------------------------------
`OPEN_BKDT` takes 11 IN parameters, has no OUT parameter and returns no result
set. Its body is a bare INSERT into a per-schema `BKDT` table. So:

    success == "the call did not raise"

There is nothing else to inspect — no status code, no row count worth reading.
That is a property of the procedure, not an omission here, and it is why a
success message says what was sent rather than quoting a reply SAP never gives.

    1 BRANCH    NVARCHAR(50)    7 RIGHTS     NVARCHAR(10)
    2 USERID    NVARCHAR(20)    8 CREATEDBY  NVARCHAR(20)
    3 TRANSTYPE INTEGER         9 CREATEDON  TIMESTAMP
    4 FROMDATE  DATE           10 DELETEDBY  NVARCHAR(10)
    5 TODATE    DATE           11 DELETEDON  TIMESTAMP
    6 TIMELIMIT TIMESTAMP

There is no ACTION parameter, and none is invented: ADD/UPDATE is recorded on
the request and never reaches SAP, exactly as in JSAP.

ONE REQUEST, ONE CALL PER COMPANY
---------------------------------
`OPEN_BKDT` takes ONE branch and writes into that company's own schema, so a
request naming two companies is two calls. That fan-out lives here and nowhere
else — the business object stays one request with one approval chain.

WHAT WAS WRONG IN JSAP AND IS FIXED HERE
-----------------------------------------
* Dates were formatted to `dd-MM-yyyy` strings, re-parsed with the SERVER's
  culture, and only then bound — so `03-04-2026` meant March 4 or April 3
  depending on the host locale. Real `date`/`datetime` objects bind here and
  never become strings.
* The branch loop kept no per-branch record, so when one company succeeded and
  another failed there was no way to tell which. Every call's parameters and
  outcome are stored — see `sap_payload` and `hana_status_text`.
* The applied-status row was written with a hardcoded "true" and only on the
  success path, so a failed write recorded nothing. Both outcomes are recorded.
* The status text was a generic application message. The exact database error
  is kept instead, because it is the only thing that says WHY a grant did not
  land.
"""
import json
import logging

from django.utils import timezone

from hana.services.connection import HANAConnection, HanaSchemaError, Queries

from backdate.models import BackDateFlow, HanaStatus
from backdate.services import sap_masters

logger = logging.getLogger(__name__)

#: `OPEN_BKDT` writes `BRANCH` as the company NAME, not JSAP's numeric code —
#: matching what JSAP actually passed after mapping 1/2/3 to OIL/BEVERAGES/MART.
_BRANCH_NAME = {'OIL': 'OIL', 'BEVERAGES': 'BEVERAGES', 'MART': 'MART'}

#: The parameter names, in declaration order. Used to label the stored payload
#: so a reader sees SAP's own names rather than a positional list.
PARAMETER_NAMES = [
    'BRANCH', 'USERID', 'TRANSTYPE', 'FROMDATE', 'TODATE', 'TIMELIMIT',
    'RIGHTS', 'CREATEDBY', 'CREATEDON', 'DELETEDBY', 'DELETEDON',
]


class HanaWriteError(Exception):
    """The SAP write failed. The message is safe to show an operator.

    It also CARRIES THE EVIDENCE — what was sent, and what SAP said back — so
    the handler that catches it can store that once its own transaction has
    ended. See `record_failure` for why it cannot be stored any earlier.
    """

    def __init__(self, message, *, payload=None, status_text=''):
        super().__init__(message)
        self.payload = payload
        self.status_text = status_text


def _rights_for(action):  # noqa: ARG001 — see the docstring
    """The `RIGHTS` value for a request's action.

    JSAP always sent the literal `'NO'` and dropped ADD/UPDATE entirely, so
    there is no observed example of any other value. `'NO'` is therefore kept
    as the default to preserve behaviour exactly.

    The ADD/UPDATE distinction an approver agreed to is recorded on the request
    and surfaced in the payload log below, so nothing is lost — but it is NOT
    invented into a HANA value that has never been seen. See migration plan
    §28.4: confirming what SAP expects here is an open business question.
    """
    return 'NO'


def build_payload(backdate, company=None):
    """The 11 positional parameters for ONE company, in declaration order.

    Kept separate from execution so it can be asserted in a test without
    touching HANA.

    EVERY VALUE COMES FROM THE REQUEST, none from the approval. That is the
    JSAP contract, verified against live data rather than read off variable
    names: `BackDateSaveInHana` builds its payload from
    `jsGetDocumentDetailUsingFlowId`, whose `createdBy` is
    `userDocument.createdBy` — the requester's numeric id — and whose
    `createdOn` is the request's own timestamp. The approver appears nowhere in
    the SAP row. Their identity is not lost: it is in
    `backdate_action_logs`, which records every decision with who made it.

    `company` names which of the request's companies this call is for; it
    defaults to the only one when a request names a single company.

    `TRANSTYPE` IS RESOLVED HERE, NOT STORED. The request holds the SAP object
    NAME; `OPEN_BKDT` needs the number. The mapping is one-to-one (75 objects,
    75 distinct non-blank names, identical in all three company schemas,
    verified against live HANA), so the lookup is exact — and doing it at call
    time means the number sent is the one SAP has NOW, never a stale copy.

    A name SAP does not know raises rather than guessing. Granting rights over
    the wrong object because a name nearly matched would be far worse than a
    refused approval the approver can see and correct.
    """
    companies = backdate.companies
    if company is None:
        if len(companies) != 1:
            raise HanaWriteError(
                'This request covers several companies, so the company must '
                'be named for each SAP call.')
        company = companies[0]
    elif company not in companies:
        raise HanaWriteError(
            f'"{company}" is not one of the companies this request asked for.')

    branch = _BRANCH_NAME.get(company)
    if not branch:
        raise HanaWriteError(
            f'"{company}" is not a company this module can write to SAP for.')

    # SAP ignores a grant with no expiry — `SBO_SP_TRANSACTIONNOTIFICATION`
    # ends all 14 of its BKDT lookups with `CURRENT_TIMESTAMP < r."timeLimit"`,
    # which against a NULL is UNKNOWN — so a missing one is refused here rather
    # than written and forgotten.
    if backdate.time_limit is None:
        raise HanaWriteError(
            'This request has no expiry, and SAP ignores rights that never '
            'lapse. It cannot be applied.')

    object_type = sap_masters.object_type_for(
        company, backdate.document_type_name)
    if object_type is None:
        raise HanaWriteError(
            f'SAP does not have a document type called '
            f'"{backdate.document_type_name}" in {company}, so these rights '
            f'cannot be applied. Correct the document type and try again.')

    return [
        branch,                              # 1  BRANCH    NVARCHAR(50)
        (backdate.sap_username or '')[:20],  # 2  USERID    NVARCHAR(20)
        object_type,                         # 3  TRANSTYPE INTEGER
        backdate.from_date,                  # 4  FROMDATE  DATE
        backdate.to_date,                    # 5  TODATE    DATE
        backdate.time_limit,                 # 6  TIMELIMIT TIMESTAMP
        _rights_for(backdate.action),        # 7  RIGHTS    NVARCHAR(10)
        str(backdate.created_by_id)[:20],    # 8  CREATEDBY NVARCHAR(20)
        backdate.created_at,                 # 9  CREATEDON TIMESTAMP
        None,                                # 10 DELETEDBY NVARCHAR(10)
        None,                                # 11 DELETEDON TIMESTAMP
    ]


def _jsonable(value):
    """One parameter as JSON, without losing what it was.

    Dates and timestamps become ISO strings; numbers stay numbers. The same
    rule the action log's `action_data` follows, so both audit columns read
    alike.
    """
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    isoformat = getattr(value, 'isoformat', None)
    return isoformat() if isoformat else str(value)


def payload_json(branch, params):
    """One call's parameters, labelled with SAP's own parameter names."""
    return {
        'branch': branch,
        'parameters': {name: _jsonable(value)
                       for name, value in zip(PARAMETER_NAMES, params)},
    }


def apply_grant(flow):
    """Write the approved grant into SAP — one `OPEN_BKDT` call per company.

    Returns the status text. Raises `HanaWriteError` if ANY call failed, having
    already recorded what each one sent and what SAP said back. The caller
    decides how loudly to report it, but the record is never left claiming a
    success that did not happen.

    PARTIAL FAILURE IS RECORDED, NOT HIDDEN. The calls are independent — SAP
    gives no cross-schema transaction — so OIL can land while BEVERAGES fails.
    `hana_status` then says FAILED for the request as a whole, because the
    requester does not have what they asked for, and `hana_status_text` says
    exactly which company succeeded so an administrator can retry the rest.

    The schema is chosen by `Queries._schema_for_branch` from settings, never
    from the request: HANA cannot bind an identifier, so a schema name is the
    one thing that gets interpolated and it must come from a checked source.
    Every VALUE binds.
    """
    backdate = flow.backdate
    calls, results, failures = [], [], []

    for company in backdate.companies:
        params = build_payload(backdate, company)
        branch = params[0]
        calls.append(payload_json(branch, params))

        try:
            schema = Queries._schema_for_branch(company)
        except HanaSchemaError as exc:
            message = (f'SAP is not configured for {company}. Ask an '
                       f'administrator to check the HANA settings.')
            results.append({'branch': branch, 'status': 'FAILED',
                            'response': message})
            failures.append(company)
            logger.warning('BKDT schema unresolved for %s: %s', company, exc)
            continue

        try:
            with HANAConnection() as conn:
                conn.execute(
                    f'CALL "{schema}"."OPEN_BKDT"(?,?,?,?,?,?,?,?,?,?,?)',
                    params)
        except Exception as exc:  # noqa: BLE001 — hdbcli raises a broad family
            # The EXACT database message is kept: it is the only thing that
            # says why the grant did not land. It carries the schema and the
            # statement, never a credential — the connection settings are not
            # part of an hdbcli error.
            logger.warning('BKDT OPEN_BKDT failed for flow=%s branch=%s: %s',
                           flow.pk, branch, exc, exc_info=True)
            results.append({'branch': branch, 'status': 'FAILED',
                            'response': f'{type(exc).__name__}: {exc}'})
            failures.append(company)
            continue

        # `OPEN_BKDT` returns nothing, so this states what was accepted rather
        # than quoting a reply that does not exist.
        results.append({
            'branch': branch, 'status': 'SUCCESS',
            'response': (f'OPEN_BKDT accepted: {backdate.sap_username}, '
                         f'{backdate.document_type_name}, '
                         f'{backdate.from_date} to {backdate.to_date}, '
                         f'expires {backdate.time_limit.isoformat()}.'),
        })

    status_text = json.dumps({'results': results}, indent=2, sort_keys=False)
    payload = {'calls': calls}

    if failures:
        # NOTHING IS WRITTEN HERE, deliberately. The final approval calls this
        # from inside the transaction holding `backdate_flow` FOR UPDATE, and
        # that transaction is about to be rolled back to keep the request
        # un-approved. Writing the failure now would either be lost in that
        # rollback, or — as an earlier version tried — be written from a second
        # connection, which then waits on the row lock its OWN caller is still
        # holding and never returns. That is a hang, not a slow write.
        #
        # So the evidence travels on the exception and is stored by the handler
        # after the rollback. See `record_failure`.
        raise HanaWriteError(
            'SAP refused the rights for ' + ', '.join(failures)
            + '. The request has NOT been approved — correct it and try '
              'again. The exact SAP response is recorded against it.',
            payload=payload, status_text=status_text)

    # A success belongs to the approval it was part of, so it is written on the
    # caller's own connection: the two commit together or neither does.
    flow.sap_payload = payload
    flow.hana_status = HanaStatus.SUCCESS
    flow.hana_status_text = status_text
    flow.save(update_fields=['sap_payload', 'hana_status', 'hana_status_text',
                             'updated_at'])
    return status_text


def record_failure(flow, error):
    """Store a refused SAP call against the flow, AFTER the rollback.

    Call this from the handler that caught `HanaWriteError`, once the
    approval's transaction has ended. By then the row lock is gone, so this is
    an ordinary write on the ordinary connection with nothing to wait on — and
    it survives, which is the entire point: the approver is told SAP refused,
    and must be able to open the request and read exactly why.

    A `.update()` rather than a `.save()`: the request stays exactly as the
    requester left it, and only the SAP columns move.

    Recording must never mask the SAP error the caller is already reporting, so
    a failure here is logged and swallowed.
    """
    payload = getattr(error, 'payload', None)
    status_text = getattr(error, 'status_text', '') or str(error)

    try:
        BackDateFlow.objects.filter(pk=flow.pk).update(
            sap_payload=payload, hana_status=HanaStatus.FAILED,
            hana_status_text=status_text, updated_at=timezone.now())
    except Exception:  # noqa: BLE001 — recording must not mask the SAP error
        logger.warning('BKDT could not record the SAP failure for flow=%s',
                       flow.pk, exc_info=True)

    # Keep the in-memory flow in step, so a caller that renders it straight
    # back does not show the state from before the attempt.
    flow.sap_payload = payload
    flow.hana_status = HanaStatus.FAILED
    flow.hana_status_text = status_text
