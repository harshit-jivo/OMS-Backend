"""Reading budget-gated drafts from SAP, and writing decisions to SAP's gate.

TWO DIRECTIONS, ONE FILE
------------------------
    read   ODRF/DRF1, OPDF/PDF4, OBTF/BTF1   -> drafts waiting for approval
           OBGS/OBGT, JDT1                     -> the month's budget and spend
    write  OMS_BUDGET_APPROVALS                -> what SAP's gate reads

Direct HANA, never the Service Layer, as `production/services/sap.py`.

WHICH SCHEMAS
-------------
`settings.BUDGET_SAP_SCHEMAS` only — never the company DBs the rest of OMS
reads. A dev checkout reads LIVE companies; budget approval must only touch
the companies switched over to OMS. The schema is the one interpolated thing
(HANA cannot bind an identifier) and it only ever comes from that setting.
Every value binds with `?`.

THE INTAKE IS A PORT
--------------------
Of `JIVO_OIL_HANADB."DRAFT_APPROVAL"` (JSAP's intake, which also reads
Beverages). Keep these in step with it, or OMS approves lines SAP's gate never
asks about, or misses ones it does:

* marketing drafts 14/15/18/19/59/60: ODRF/DRF1, key `DRF1.LineNum`
* outgoing payment drafts 46: OPDF/PDF4, key `PDF4.LineId`
* journal vouchers 28: OBTF/BTF1, `BatchNum` as the DocEntry, key `Line_ID`;
  debit lines only, salary accounts 5630001–5630016 excluded
* waiting = SAP's approval request open (`OWDD.ProcesStat = 'Y'`,
  `WDD1.Status` 'Y'; Oil's payments also 'P'), or the voucher batch open
* account parent 5610000–5690000, or account 5100008
* `OcrCode3 != 'Sal CF'` — which also drops a line with no budget code
* dated on or after 2025-04-01
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from hana.services.connection import HANAConnection

logger = logging.getLogger(__name__)

#: OMS's own gate table (`budget_gate`), in each company schema.
GATE_TABLE = 'OMS_BUDGET_APPROVALS'

EXPENSE_PARENTS = ('5690000', '5680000', '5670000', '5660000', '5650000',
                   '5640000', '5630000', '5620000', '5610000')
EXTRA_ACCOUNTS = ('5100008',)
SALARY_ACCOUNTS = tuple(f'56300{n:02d}' for n in (1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16))
MARKETING_TYPES = (14, 15, 18, 19, 59, 60)
SINCE = '2025-04-01'
#: `WDD1.Status` values that count as "waiting" for a payment draft. Oil's
#: branch of DRAFT_APPROVAL also takes 'P'; Beverages' does not.
PAYMENT_WDD_STATUSES = {'OIL': ('Y', 'P')}

#: Gate statuses OMS writes.
APPROVED, MOVED_ON, REJECTED = 'A', 'V', 'R'


class SapUnavailable(Exception):
    """HANA could not be read. Raised, never turned into "nothing to do"."""


class SapWriteError(Exception):
    """A decision could not be written to SAP's gate. The decision stands."""


def schemas():
    """{company: schema} this module works on. Empty = switched off."""
    return dict(getattr(settings, 'BUDGET_SAP_SCHEMAS', {}) or {})


def schema_for(company):
    schema = schemas().get(company)
    if not schema:
        raise SapUnavailable(f'Budget approval is not switched on for {company} '
                             f'(BUDGET_SAP_SCHEMAS).')
    return schema


def _expense(alias, acct='AcctCode'):
    parents = ', '.join(f"'{p}'" for p in EXPENSE_PARENTS)
    extra = ', '.join(f"'{a}'" for a in EXTRA_ACCOUNTS)
    return f'({alias}."FatherNum" IN ({parents}) OR {alias}."{acct}" IN ({extra}))'


def intake_sql(schema, company):
    """The UNION of the three draft sources, one row per gated line."""
    statuses = ', '.join(f"'{s}'" for s in PAYMENT_WDD_STATUSES.get(company, ('Y',)))
    types = ', '.join(str(t) for t in MARKETING_TYPES)
    salary = ', '.join(f"'{a}'" for a in SALARY_ACCOUNTS)
    return f'''
SELECT DISTINCT RF."ObjType" "obj_type", RF."DocEntry" "draft_entry", RF."DocNum" "doc_num",
       F1."LineNum" "line_num", F1."VisOrder" "vis_order", F1."AcctCode" "acct_code", OA."AcctName" "acct_name",
       RF."CardCode" "card_code", RF."CardName" "card_name", F1."OcrCode2" "effect_month",
       F1."OcrCode3" "budget_code", F1."OcrCode4" "sub_budget_code", RF."DocDate" "doc_date",
       F1."LineTotal" "amount", OU."U_NAME" "created_by", TO_NVARCHAR(RF."Comments") "comments",
       TO_NVARCHAR(F1."U_Remarks") "remarks"
FROM "{schema}"."ODRF" RF
JOIN "{schema}"."DRF1" F1 ON F1."DocEntry" = RF."DocEntry" AND F1."ObjType" = RF."ObjType"
JOIN "{schema}"."OACT" OA ON OA."AcctCode" = F1."AcctCode"
JOIN "{schema}"."OWDD" OWD ON OWD."DraftEntry" = RF."DocEntry"
JOIN "{schema}"."WDD1" WD ON WD."WddCode" = OWD."WddCode"
LEFT JOIN "{schema}"."OUSR" OU ON OU."USERID" = OWD."OwnerID"
WHERE RF."ObjType" IN ({types}) AND WD."Status" = 'Y' AND OWD."ProcesStat" = 'Y'
  AND RF."DocDate" >= '{SINCE}' AND {_expense('OA')} AND F1."OcrCode3" != 'Sal CF'
UNION ALL
SELECT DISTINCT RF."ObjType", RF."DocEntry", RF."DocNum",
       F1."LineId", F1."LineId", F1."AcctCode", OA."AcctName",
       RF."CardCode", RF."CardName", F1."OcrCode2",
       F1."OcrCode3", F1."OcrCode4", RF."DocDate",
       F1."SumApplied", OU."U_NAME", TO_NVARCHAR(RF."Comments"), TO_NVARCHAR(F1."U_Remarks")
FROM "{schema}"."OPDF" RF
JOIN "{schema}"."PDF4" F1 ON F1."DocNum" = RF."DocEntry" AND F1."ObjType" = RF."ObjType"
JOIN "{schema}"."OACT" OA ON OA."AcctCode" = F1."AcctCode"
JOIN "{schema}"."OWDD" OWD ON OWD."DraftEntry" = RF."DocEntry"
JOIN "{schema}"."WDD1" WD ON WD."WddCode" = OWD."WddCode"
LEFT JOIN "{schema}"."OUSR" OU ON OU."USERID" = OWD."OwnerID"
WHERE RF."ObjType" = 46 AND WD."Status" IN ({statuses}) AND OWD."ProcesStat" = 'Y'
  AND RF."DocDate" >= '{SINCE}' AND {_expense('OA')} AND F1."OcrCode3" != 'Sal CF'
UNION ALL
SELECT DISTINCT 28, B."BatchNum", B."BatchNum",
       A."Line_ID", A."Line_ID", A."Account", C."AcctName",
       '', '', A."OcrCode2",
       A."OcrCode3", A."OcrCode4", B."RefDate",
       A."Debit", OU."U_NAME", TO_NVARCHAR(B."Memo"), TO_NVARCHAR(A."LineMemo")
FROM "{schema}"."BTF1" A
JOIN "{schema}"."OBTF" B ON B."BatchNum" = A."BatchNum"
JOIN "{schema}"."OACT" C ON C."AcctCode" = A."Account"
LEFT JOIN "{schema}"."OUSR" OU ON OU."USERID" = B."UserSign"
WHERE B."BtfStatus" = 'O' AND {_expense('C', 'AcctCode')} AND C."AcctCode" NOT IN ({salary})
  AND A."Debit" <> 0 AND B."RefDate" >= '{SINCE}' AND A."OcrCode3" != 'Sal CF'
'''


def gate_status_sql(schema):
    """What the gate already holds for these drafts: lines JSAP approved are not taken in again."""
    return f'SELECT "ObjType" "obj_type", "DocEntry" "draft_entry", "LineNum" "line_num", "ApprovedStatus" "status" ' \
           f'FROM "{schema}"."{GATE_TABLE}" WHERE "ApprovedStatus" = \'A\''


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def _date(value):
    return value.date() if hasattr(value, 'date') else value


def gated_lines(company):
    """Every draft line SAP's gate is waiting on in `company`, plus the lines already approved.

    Returns `(lines, approved)`: `lines` as dicts, `approved` the set of
    `(obj_type, draft_entry, line_num)` the gate already holds 'A' for. Raises
    `SapUnavailable` — never `[]` — when SAP cannot be read: JSAP's production
    feed reported success for weeks while reading nothing.
    """
    schema = schema_for(company)
    try:
        with HANAConnection() as conn:
            rows = conn.execute(intake_sql(schema, company))
            approved = {(int(r['obj_type']), int(r['draft_entry']), int(r['line_num']))
                        for r in conn.execute(gate_status_sql(schema))}
    except Exception as exc:  # noqa: BLE001
        raise SapUnavailable(f'Could not read {company} drafts from SAP: {exc}') from exc
    lines = []
    for r in rows:
        lines.append({
            'obj_type': int(r['obj_type']), 'draft_entry': int(r['draft_entry']),
            'doc_num': int(r['doc_num']) if r['doc_num'] is not None else None,
            'line_num': int(r['line_num']),
            'vis_order': int(r['vis_order']) if r['vis_order'] is not None else None,
            'acct_code': (r['acct_code'] or '').strip(), 'acct_name': (r['acct_name'] or '')[:200],
            'card_code': (r['card_code'] or '')[:50], 'card_name': (r['card_name'] or '')[:200],
            'effect_month': (r['effect_month'] or '')[:50],
            'budget_code': (r['budget_code'] or '').strip(), 'sub_budget_code': (r['sub_budget_code'] or '').strip(),
            'doc_date': _date(r['doc_date']), 'amount': _money(r['amount']),
            'created_by': (r['created_by'] or '')[:100], 'comments': r['comments'] or '',
            'remarks': r['remarks'] or '',
        })
    return lines, approved


# ---------------------------------------------------------------------------
# The month's budget and spend — read only to decide auto-approval
# ---------------------------------------------------------------------------

def month_budget(company, budget_code, day):
    """SAP's budget for `budget_code` covering `day`, or None when SAP has none.

    `OBGS` scenarios carry their head (`OcrCode3`) and period
    (`U_UNE_FRDT`–`U_UNE_TODT`); `OBGT` the amounts — as DRAFT_APPROVAL reads
    them. None, never 0: a month without a budget is unconfigured, and reading
    it as zero is what killed JSAP's auto-approval in March 2026.
    """
    schema = schema_for(company)
    sql = (f'SELECT SUM(B."DebLTotal") "total", COUNT(*) "n" FROM "{schema}"."OBGS" A '
           f'JOIN "{schema}"."OBGT" B ON B."Instance" = A."AbsId" '
           f'WHERE ? BETWEEN A."U_UNE_FRDT" AND A."U_UNE_TODT" AND A."OcrCode3" = ?')
    try:
        with HANAConnection() as conn:
            row = conn.execute(sql, [day, budget_code])[0]
    except Exception as exc:  # noqa: BLE001
        raise SapUnavailable(f'Could not read the {company} budget: {exc}') from exc
    if not row['n'] or row['total'] is None:
        return None
    return _money(row['total'])


def month_spend(company, budget_code, day):
    """What `company` has posted against `budget_code` in `day`'s month.

    Indirect expenses (grandparent 5600000, except 5680014), as DRAFT_APPROVAL's
    `Current_month_Posted_Amount`.
    """
    schema = schema_for(company)
    start = day.replace(day=1)
    sql = (f'SELECT SUM(A."Debit" - A."Credit") "total" FROM "{schema}"."JDT1" A '
           f'JOIN "{schema}"."OJDT" B ON B."TransId" = A."TransId" '
           f'JOIN "{schema}"."OACT" C ON C."AcctCode" = A."Account" '
           f'JOIN "{schema}"."OACT" D ON D."AcctCode" = C."FatherNum" '
           f"WHERE D.\"FatherNum\" = '5600000' AND C.\"AcctCode\" <> '5680014' AND A.\"OcrCode3\" = ? "
           f'AND B."RefDate" >= ? AND B."RefDate" < ADD_MONTHS(?, 1)')
    try:
        with HANAConnection() as conn:
            row = conn.execute(sql, [budget_code, start, start])[0]
    except Exception as exc:  # noqa: BLE001
        raise SapUnavailable(f'Could not read the {company} spend: {exc}') from exc
    return _money(row['total'])


# ---------------------------------------------------------------------------
# Writing the decision to SAP's gate
# ---------------------------------------------------------------------------

def write_item(item, status, *, decided_by='oms', remarks=''):
    """UPSERT the item's lines into `OMS_BUDGET_APPROVALS`, then read them back.

    `status` is A (approved — SAP may post), V (a stage approved, more follow)
    or R (rejected). An upsert, so a retry converges instead of colliding.
    The read-back is JSAP's one good habit (`jsSyncBudgetToHanaDraftApproval`
    threw when the rows did not match): a write that "succeeded" but left the
    gate holding something else is a failure.

    On failure the OMS decision stands; what SAP said is recorded on the item
    and `SapWriteError` raised for the caller to report.
    """
    draft = item.draft
    schema = schema_for(item.company)
    table = f'"{schema}"."{GATE_TABLE}"'
    verified = 'V' if status == APPROVED else None
    now = timezone.now()
    lines = list(item.lines.all())
    sql = (f'UPSERT {table} ("ObjType","DocEntry","LineNum","VisOrder","AcctCode","CardCode","DocDate",'
           f'"BUDGET","SUB_BUDGET","AMOUNT","ApprovedStatus","VerifiedStatus","Remarks","OmsRequestId",'
           f'"DecidedBy","DecidedAt","UpdatedAt") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) WITH PRIMARY KEY')
    try:
        with HANAConnection() as conn:
            for line in lines:
                conn.execute(sql, [
                    draft.obj_type, draft.draft_entry, line.line_num, line.vis_order, line.acct_code,
                    draft.card_code, draft.doc_date, line.budget_code, line.sub_budget_code, line.amount,
                    status, verified, (remarks or '')[:5000], item.pk, decided_by[:150], now, now])
            back = conn.execute(
                f'SELECT "LineNum" "line", "ApprovedStatus" "status" FROM {table} '
                f'WHERE "ObjType" = ? AND "DocEntry" = ?', [draft.obj_type, draft.draft_entry])
        held = {int(r['line']): r['status'] for r in back}
        wrong = [ln.line_num for ln in lines if held.get(ln.line_num) != status]
        if wrong:
            raise SapWriteError(f'SAP holds a different status for lines {wrong} after the write.')
    except Exception as exc:  # noqa: BLE001
        text = f'{type(exc).__name__}: {exc}'
        logger.warning('BUDGET: gate write %s failed for item %s: %s', status, item.pk, text)
        item.sap_status, item.sap_status_text = 'FAILED', text
        item.save(update_fields=['sap_status', 'sap_status_text', 'updated_at'])
        raise SapWriteError(text) from exc
    item.sap_status = 'SUCCESS'
    item.sap_status_text = f'{len(lines)} line(s) = {status} in {schema}.{GATE_TABLE}'
    item.sap_written_at = now
    item.save(update_fields=['sap_status', 'sap_status_text', 'sap_written_at', 'updated_at'])
    return item.sap_status_text


def clear_draft(company, obj_type, draft_entry):
    """Remove a draft's rows from the gate: it changed in SAP, so its old decisions no longer apply."""
    schema = schema_for(company)
    try:
        with HANAConnection() as conn:
            conn.execute(f'DELETE FROM "{schema}"."{GATE_TABLE}" WHERE "ObjType" = ? AND "DocEntry" = ?',
                         [obj_type, draft_entry])
    except Exception as exc:  # noqa: BLE001
        raise SapWriteError(f'Could not clear the changed draft from SAP: {exc}') from exc
