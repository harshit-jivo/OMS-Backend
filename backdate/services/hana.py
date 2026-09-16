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

STAMPING THE OMS REQUEST ID ONTO THE SAP ROW
--------------------------------------------
`BKDT` has an `id` column that `OPEN_BKDT` never writes, so every row it has
ever inserted carries `id = NULL` — 1,292 of them across the three live
schemas, with nothing to tie a SAP row back to the request that asked for it.
Finding one means matching on `userid` + `createdOn` and hoping.

So after a successful call this module stamps the OMS request id into that
column with a follow-up `UPDATE`. Verified safe before doing it:

* `id` is a plain nullable INTEGER with no default and no generated value.
* `BKDT` has NO primary key, NO unique index and NO constraint of any kind,
  so a value there cannot collide with or violate anything.
* `SBO_SP_TRANSACTIONNOTIFICATION` — the posting validator that decides whether
  a back-dated document is allowed — contains 14 BKDT lookups and reads the
  `id` column in NONE of them. Writing it cannot change what SAP permits.
* The only 6 non-NULL ids in live data are all the literal `70`, left by some
  older tool. They are not a scheme to preserve, and the `"id" IS NULL` guard
  below means they are never touched.

It is a second statement rather than a 12th parameter because `OPEN_BKDT` is
SHARED WITH JSAP: changing its signature would need a SAP-side release and
would still leave JSAP writing NULL. And it is BEST EFFORT — the rights are
already granted by the time it runs, so failing to label a row must never fail
the grant or unsay a success that really happened.

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
import datetime
import json
import logging
import zoneinfo

from django.conf import settings
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


#: The wall clock SAP's own `CURRENT_TIMESTAMP` runs on.
#:
#: `BKDT."timeLimit"` is a bare TIMESTAMP with no zone, and SAP's posting
#: validator compares it against `CURRENT_TIMESTAMP` — the SERVER's clock, which
#: is UTC+05:30. JSAP has always written local time here: its own
#: `userDocument.timeLimit` and the HANA row it produced are identical to the
#: second, verified on live rows.
#:
#: OMS stores timestamps as UTC, so binding them unconverted made every grant
#: expire 5 hours 30 minutes EARLY — an approved request that stops working
#: before the user thinks it has. This is the conversion that fixes it.
SAP_TIME_ZONE = getattr(settings, 'SAP_TIME_ZONE', 'Asia/Kolkata')


def sap_offset():
    """SAP's offset from UTC, as a `timedelta`."""
    now = datetime.datetime.now(zoneinfo.ZoneInfo(SAP_TIME_ZONE))
    return now.utcoffset()


def to_sap_clock(value, offset=None):
    """An aware timestamp as SAP's own wall clock, naive.

    A naive value is passed through untouched — it is already somebody's local
    time and guessing which would be worse than leaving it.
    """
    if value is None or timezone.is_naive(value):
        return value
    offset = sap_offset() if offset is None else offset
    return value.astimezone(datetime.timezone.utc).replace(tzinfo=None) + offset


def _check_sap_clock(conn):
    """Warn if SAP's real clock disagrees with `SAP_TIME_ZONE`.

    The conversion above is driven by configuration so it stays deterministic
    and testable; this is the check that the configuration is still true. A
    silent drift here would put every expiry out by hours, so it is worth one
    cheap query per call to find out loudly.
    """
    try:
        row = conn.execute('SELECT CURRENT_TIMESTAMP AS "L", '
                           'CURRENT_UTCTIMESTAMP AS "U" FROM DUMMY')[0]
        actual = row['L'] - row['U']
    except Exception:  # noqa: BLE001 — never worth failing a grant over
        return
    expected = sap_offset()
    # Half a minute of slack: the two values are read a moment apart.
    if abs((actual - expected).total_seconds()) > 30:
        logger.error(
            'BKDT: SAP_TIME_ZONE is %s (UTC%+.1fh) but the HANA server clock '
            'is UTC%+.1fh. Every timeLimit written is out by the difference, '
            'so grants will expire at the wrong time until this is corrected.',
            SAP_TIME_ZONE, expected.total_seconds() / 3600,
            actual.total_seconds() / 3600)


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

    # Both timestamps go in on SAP's WALL CLOCK, not UTC. `timeLimit` is
    # compared against the server's `CURRENT_TIMESTAMP`, so a UTC value on a
    # UTC+05:30 server expires five and a half hours early; `createdOn` is
    # converted with it so the two read consistently and match JSAP's rows.
    offset = sap_offset()
    return [
        branch,                              # 1  BRANCH    NVARCHAR(50)
        (backdate.sap_username or '')[:20],  # 2  USERID    NVARCHAR(20)
        object_type,                         # 3  TRANSTYPE INTEGER
        backdate.from_date,                  # 4  FROMDATE  DATE
        backdate.to_date,                    # 5  TODATE    DATE
        to_sap_clock(backdate.time_limit, offset),   # 6  TIMELIMIT TIMESTAMP
        _rights_for(backdate.action),        # 7  RIGHTS    NVARCHAR(10)
        str(backdate.created_by_id)[:20],    # 8  CREATEDBY NVARCHAR(20)
        to_sap_clock(backdate.created_at, offset),   # 9  CREATEDON TIMESTAMP
        None,                                # 10 DELETEDBY NVARCHAR(10)
        None,                                # 11 DELETEDON TIMESTAMP
    ]


#: Schemas where tagging has been refused, so it is not tried again.
#:
#: The connected HANA user holds UPDATE on five of the six BKDT schemas but NOT
#: on live `JIVO_BEVERAGES_HANADB`, and that grant is not going to be given.
#: Without this, every BEVERAGES approval would run a statement that cannot
#: succeed and log a warning about it — a permanent stream of noise describing
#: a settled fact.
#:
#: Learned rather than configured, because a hard-coded schema list would be
#: another copy of the deployment's privileges to keep in step. Process-lifetime
#: only, so a restart re-tries: if the grant is ever given, tagging simply
#: starts working. Same pattern and same reasoning as `_COLUMN_EXISTS_CACHE` in
#: `hana.services.connection`.
_TAGGING_REFUSED = set()


def _stamp_request_id(conn, schema, backdate, params):
    """Write the OMS request id onto the row `OPEN_BKDT` has just inserted.

    Returns True when a row was tagged. NEVER raises: the grant is already in
    SAP by the time this runs, and a missing label is a smaller problem than an
    approval reported as failed after the rights were actually given.

    The row is found by EVERY column `OPEN_BKDT` was just given — branch, SAP
    user, object type, both dates, the expiry, rights, createdBy and createdOn.
    Matching on fewer would be enough almost always, and "almost" is the
    problem: see the note in the body.

    `"id" IS NULL` means this only ever fills a blank. It cannot overwrite a
    value somebody else put there, and if a request somehow wrote two rows
    (SAP has no unique constraint to prevent it) BOTH get the same id, which
    is exactly how you would want a duplicate to show up.

    VERIFIED, because it is the property that matters: `HANAConnection.execute`
    commits a statement that returns no result set, so the `CALL` above has
    already committed by the time this runs. The rollback that follows a failed
    UPDATE therefore undoes nothing — a refused tag cannot take the grant with
    it. Tested against a real failure, not reasoned about.
    """
    if schema in _TAGGING_REFUSED:
        return False

    try:
        # MATCH THE WHOLE ROW, not a convenient corner of it.
        #
        # `("userid", "createdOn")` alone is NOT unique in this table: JSAP
        # writes `createdOn` truncated to the whole second (536 of 641 live OIL
        # rows have no sub-second part, some are 00:00:00), and 44 live OIL
        # pairs are already duplicated. An OMS timestamp landing exactly on a
        # whole second is improbable, not impossible — and "improbable" is not
        # a good enough reason to put a stray id on somebody else's row.
        #
        # So every column `OPEN_BKDT` was just given is in the WHERE clause. A
        # false match would have to be identical in all nine, which makes it
        # indistinguishable from the row we wrote anyway.
        branch, userid, transtype = params[0], params[1], params[2]
        from_date, to_date, time_limit = params[3], params[4], params[5]
        rights, created_by, created_on = params[6], params[7], params[8]
        conn.execute(
            f'UPDATE "{schema}"."BKDT" SET "id" = ? '
            f'WHERE "id" IS NULL '
            f'  AND "branch" = ? AND "userid" = ? AND "transtype" = ? '
            f'  AND "fromDate" = ? AND "toDate" = ? AND "timeLimit" = ? '
            f'  AND "rights" = ? AND "createdBy" = ? AND "createdOn" = ?',
            [int(backdate.pk), branch, userid, transtype, from_date, to_date,
             time_limit, rights, created_by, created_on])
        return True
    except Exception:  # noqa: BLE001 — a label is never worth a failed grant
        _TAGGING_REFUSED.add(schema)
        logger.warning(
            'BKDT granted the rights for request=%s in %s but could not tag '
            'the SAP row with its id, and will not try %s again until this '
            'process restarts. Grants there stay correct; their rows keep '
            'id=NULL and are found by "userid" + "createdOn". To enable '
            'tagging, UPDATE on that schema is what is missing.',
            backdate.pk, schema, schema, exc_info=True)
        return False


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

        stamped = None
        try:
            with HANAConnection() as conn:
                _check_sap_clock(conn)
                conn.execute(
                    f'CALL "{schema}"."OPEN_BKDT"(?,?,?,?,?,?,?,?,?,?,?)',
                    params)

                # PAST THIS LINE THE GRANT IS IN SAP. Everything after it is
                # labelling, and labelling must never be able to turn a real
                # grant into a reported failure — so it is caught HERE, at the
                # call site, and not only inside the helper. Belt and braces
                # on purpose: the failure this guards against is telling an
                # approver the rights were refused after SAP has given them.
                try:
                    stamped = _stamp_request_id(conn, schema, backdate, params)
                except Exception:  # noqa: BLE001 — see above
                    logger.warning(
                        'BKDT tagged nothing for request=%s in %s',
                        backdate.pk, schema, exc_info=True)
                    stamped = False
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
        #
        # ONE SENTENCE. Where the row can be found is a FIELD (`sap_row_id`),
        # not prose: an approver reading this wants to know the grant landed,
        # and a SQL query pasted into the middle of that buried it. The query
        # belongs in the runbook — §18.4 — where somebody is actually looking
        # for one.
        results.append({
            'branch': branch,
            'status': 'SUCCESS',
            'response': (f'OPEN_BKDT accepted: {backdate.sap_username}, '
                         f'{backdate.document_type_name}, '
                         f'{backdate.from_date} to {backdate.to_date}, '
                         f'expires {backdate.time_limit:%Y-%m-%d %H:%M}.'),
            # The id on the SAP row, or None where the connection cannot write
            # that column (live BEVERAGES). None means "find it by SAP user and
            # createdOn" and the page says so in its own words.
            'sap_row_id': backdate.pk if stamped else None,
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
