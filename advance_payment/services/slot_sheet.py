"""The payment slot sheet (user, 2026-10-22): the Final desk's Excel of what
was paid, in the layout the accounts team keeps by hand.

One row per request whose voucher SAP holds (posted at Final), in blocks of
one day and one company:

    VOUCHER NO   the OMS request number (not SAP's)
    OPENING      the payee's SAP balance just BEFORE this payment
    PAYMENT      what left the bank (the request less any TDS deducted)
    BALANCE      the payee's SAP balance just AFTER it
    BRANCH       HR for the plant's budget heads (Factory, Del Bkhp, FACT_COM), DL for the rest
    AGAINST      PO / INVOICE / ADVANCE / ON ACCOUNT / AS PER SAP
    APPROVAL     who approved it at the approval stages, "N/A" for none
    DEBIT SINCE  days the payee had stood in debit before the payment, "N/A" when not in debit
    DEBIT FROM   the last four digits of the bank account the money left

The balances are read from the payee's SAP ledger (JDT1) up to the payment's
own journal transaction, so a sheet downloaded later still says what the
balance was at the time. Sign as SAP's: debit (owed by them) positive, credit
(payable to them) negative. Read only.
"""
import io
from collections import defaultdict
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from advance_payment.models import (
    LogAction, PaymentAgainst, RequestLog, SapVoucher, StageRole, VoucherObject, VoucherStatus,
)
from advance_payment.services import clock
from advance_payment.services import sap as sap_service
from advance_payment.services.sap import SapUnavailable, _run, _s, _schema

#: The plant's budget heads (user, 2026-10-22): branch HR; every other head DL.
HR_BUDGETS = frozenset({'FACTORY', 'DEL BKHP', 'FACT_COM'})

AGAINST = {
    PaymentAgainst.AGAINST_PO: 'PO',
    PaymentAgainst.AGAINST_BILL: 'INVOICE',
    PaymentAgainst.ADVANCE: 'ADVANCE',
    PaymentAgainst.ON_ACCOUNT: 'ON ACCOUNT',
    PaymentAgainst.AGAINST_LEDGER: 'AS PER SAP',
    PaymentAgainst.AGAINST_JOURNAL: 'AS PER SAP',
    PaymentAgainst.DIRECT_EXPENSE: 'EXPENSE',
    PaymentAgainst.INDIRECT_EXPENSE: 'EXPENSE',
    PaymentAgainst.ALL: 'ALL',
}
UNIT = {'OIL': 'OIL', 'BEVERAGES': 'BEV', 'MART': 'MART'}

HEADERS = ('VOUCHER NO', 'SR.NO', 'VENDOR NAME', 'BANK ACCOUNT', 'IFSC CODE', 'OPENING', 'UNIT', 'PAYMENT',
           'NATURE OF PAYMENT', 'BRANCH', 'AGAINST', 'BALANCE', 'APPROVAL', 'DEBIT SINCE', 'DEBIT FROM')
WIDTHS = (14, 7, 38, 20, 14, 14, 7, 14, 32, 8, 12, 14, 30, 12, 12)
MONEY = ('OPENING', 'PAYMENT', 'BALANCE')


def branch_of(budget_code):
    return 'HR' if (budget_code or '').strip().upper() in HR_BUDGETS else 'DL'


def against_of(advance):
    if advance.payment_against == PaymentAgainst.OTHER:
        return (advance.payment_against_other or 'OTHER').upper()
    return AGAINST.get(advance.payment_against, advance.payment_against)


def vouchers(scope):
    """The live posted payment vouchers: `scope` 'today' (posted today, India) or 'all'.

    An Expense's A/P invoice is no payment: it is not on the sheet.
    """
    rows = (SapVoucher.objects.filter(status=VoucherStatus.POSTED, replaced_by__isnull=True)
            .exclude(sap_object=VoucherObject.PURCHASE_INVOICE)
            .select_related('request', 'request__payout')
            .prefetch_related('request__payout__lines')
            .order_by('posted_on', 'id'))
    out = []
    today = clock.today()
    for v in rows:
        day = v.posted_on.astimezone(clock.INDIA).date()
        if scope == 'today' and day != today:
            continue
        out.append((day, v))
    return out


# ---------------------------------------------------------------------------
# SAP: the payment's transaction, the payee's ledger, the paying bank
# ---------------------------------------------------------------------------

_PAYMENT_TRANS_SQL = 'SELECT "DocEntry" AS "doc_entry", "TransId" AS "trans" FROM "{schema}"."OVPM" WHERE "DocEntry" IN ({marks})'

_LEDGER_SQL = '''
    SELECT "TransId" AS "trans", MIN("RefDate") AS "day", SUM("Debit" - "Credit") AS "net"
    FROM "{schema}"."JDT1" WHERE "ShortName" = ? AND "TransId" <= ?
    GROUP BY "TransId" ORDER BY "TransId"
'''

_COMPANY_NAME_SQL = 'SELECT "CompnyName" AS "name" FROM "{schema}"."OADM"'


def _payment_trans(company, entries):
    """`{OVPM DocEntry: its journal TransId}`."""
    entries = sorted({int(e) for e in entries if e})
    if not entries:
        return {}
    sql = _PAYMENT_TRANS_SQL.format(schema=_schema(company), marks=', '.join('?' * len(entries)))
    return {int(r['doc_entry']): int(r['trans']) for r in _run(company, sql, entries, 'payment transactions')}


def _ledger(company, short_name, up_to):
    """The payee's ledger up to TransId `up_to`: `[(trans, day, net)]`, oldest first."""
    sql = _LEDGER_SQL.format(schema=_schema(company))
    return [(int(r['trans']), r['day'], Decimal(str(r['net'] or 0)))
            for r in _run(company, sql, [short_name, int(up_to)], 'payee ledger')]


def standing_before(lines, first_trans, on_day):
    """`(opening, debit_days)` just before transaction `first_trans`.

    `debit_days`: how long the running balance had stood in debit (owed by
    the payee) up to `on_day`, the day it was paid; None when not in debit.
    Paise left over by rounding count as nothing (`sap_relations.standing`).
    """
    running, since = Decimal('0'), None
    for trans, day, net in lines:
        if trans >= first_trans:
            break
        before = running.quantize(Decimal('0.01'))
        running += net
        after = running.quantize(Decimal('0.01'))
        if after == 0:
            since = None
        elif before == 0 or (before > 0) != (after > 0):
            since = day
    opening = running.quantize(Decimal('0.01'))
    if opening <= 0 or since is None:
        return opening, None
    since = since.date() if hasattr(since, 'date') else since
    return opening, (on_day - since).days


def balance_through(lines, last_trans):
    return sum((net for trans, _day, net in lines if trans <= last_trans), Decimal('0')).quantize(Decimal('0.01'))


def _last4(number):
    digits = ''.join(ch for ch in str(number or '') if ch.isalnum())
    return digits[-4:] if digits else ''


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------

def _approvers(request_ids):
    """`{request_id: 'NAME, NAME'}`: who approved it at its approval stages
    (not Payment, CA, Audit, Final), in order, each once."""
    out = defaultdict(list)
    logs = (RequestLog.objects.filter(request_id__in=list(request_ids), action=LogAction.APPROVED)
            .select_related('actor').order_by('created_on', 'id'))
    for row in logs:
        if StageRole.of(row.stage_name) != StageRole.APPROVAL or row.actor is None:
            continue
        name = (getattr(row.actor, 'name', '') or row.actor.username).strip().upper()
        if name and name not in out[row.request_id]:
            out[row.request_id].append(name)
    return {k: ', '.join(v) for k, v in out.items()}


def build_rows(entries):
    """`[{day, company, cells: {header: value}}]` for `vouchers()`'s entries.

    SAP is read once per company and payee; if it cannot be read the row
    still stands, its Opening, Balance and Debit Since blank.
    """
    approvers = _approvers(v.request_id for _d, v in entries)
    by_company = defaultdict(list)
    for day, v in entries:
        by_company[v.request.company].append((day, v))

    banks, trans, names = {}, {}, {}
    for company, items in by_company.items():
        try:
            banks[company] = {b['gl_account']: b['account_number'] for b in sap_service.house_banks(company)}
        except (SapUnavailable, ValueError):
            banks[company] = {}
        try:
            trans[company] = _payment_trans(company, [v.sap_doc_entry for _d, v in items
                                                      if v.sap_object == VoucherObject.OUTGOING_PAYMENT])
        except (SapUnavailable, ValueError):
            trans[company] = {}
        try:
            got = _run(company, _COMPANY_NAME_SQL.format(schema=_schema(company)), [], 'company name')
            names[company] = _s(got[0]['name']).upper() if got else company
        except (SapUnavailable, ValueError):
            names[company] = company

    def trans_of(v):
        if v.sap_object == VoucherObject.JOURNAL_ENTRY:
            return v.sap_doc_entry
        return trans[v.request.company].get(v.sap_doc_entry)

    # Each payee's ledger once, up to the latest of their payments on the sheet.
    ledgers = {}
    bounds = defaultdict(int)
    for _day, v in entries:
        t = trans_of(v)
        if t and v.request.partner_code:
            key = (v.request.company, v.request.partner_code)
            bounds[key] = max(bounds[key], t, v.tds_trans_id or 0)
    for (company, code), up_to in bounds.items():
        try:
            ledgers[(company, code)] = _ledger(company, code, up_to)
        except (SapUnavailable, ValueError):
            ledgers[(company, code)] = None

    rows = []
    for day, v in entries:
        a = v.request
        payout = getattr(a, 'payout', None)
        lines = list(payout.lines.all()) if payout else []
        tds = payout.tds_amount if payout else Decimal('0')
        paid = Decimal(str(a.amount)) - (tds or 0)
        opening = balance = debit_days = None
        t = trans_of(v)
        ledger = ledgers.get((a.company, a.partner_code))
        if t and ledger is not None:
            ours = [x for x in (t, v.tds_trans_id) if x]
            opening, debit_days = standing_before(ledger, min(ours), day)
            balance = balance_through(ledger, max(ours))
        paid_from = []
        for line in lines:
            label = 'CASH' if line.method == 'CASH' else _last4(banks[a.company].get(line.from_account)
                                                              or line.from_account)
            if label and label not in paid_from:
                paid_from.append(label)
        rows.append({
            'day': day, 'company': a.company, 'company_name': names.get(a.company, a.company),
            'cells': {
                'VOUCHER NO': a.request_no,
                'VENDOR NAME': (a.partner_name or '').upper(),
                'BANK ACCOUNT': payout.to_account_number if payout else '',
                'IFSC CODE': payout.to_ifsc if payout else '',
                'OPENING': opening,
                'UNIT': UNIT.get(a.company, a.company),
                'PAYMENT': paid,
                'NATURE OF PAYMENT': (a.purpose_label or a.get_request_type_display()).upper(),
                'BRANCH': branch_of(a.budget_code),
                'AGAINST': against_of(a),
                'BALANCE': balance,
                'APPROVAL': approvers.get(a.pk) or 'N/A',
                'DEBIT SINCE': f'{debit_days} DAYS' if debit_days is not None else 'N/A',
                'DEBIT FROM': ', '.join(paid_from),
            },
        })
    return rows


# ---------------------------------------------------------------------------
# The workbook
# ---------------------------------------------------------------------------

_BLUE = PatternFill('solid', fgColor='2F5597')
_WHITE_BOLD = Font(name='Arial', size=9, bold=True, color='FFFFFF')
_BODY = Font(name='Arial', size=9)
_THIN = Side(style='thin', color='808080')
_BOX = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_MONEY_FORMAT = '#,##0.00;-#,##0.00;"-"'


def workbook(rows):
    """The sheet as `.xlsx` bytes: a block per day and company, oldest first."""
    book = Workbook()
    sheet = book.active
    sheet.title = 'Payment Slot'
    for i, width in enumerate(WIDTHS, start=1):
        sheet.column_dimensions[get_column_letter(i)].width = width

    blocks = defaultdict(list)
    for row in rows:
        blocks[(row['day'], row['company'])].append(row)
    r = 1
    if not blocks:
        sheet.cell(row=1, column=1, value='No payments posted to SAP.').font = _BODY
    for (day, _company), items in sorted(blocks.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        # Title row: SLOT | 1 | DATE - dd/mm/yyyy | company
        title = [('SLOT', 1, 1), (1, 2, 2), (f'DATE - {day:%d/%m/%Y}', 3, 5), (items[0]['company_name'], 6, 15)]
        for value, start, end in title:
            cell = sheet.cell(row=r, column=start, value=value)
            if end > start:
                sheet.merge_cells(start_row=r, start_column=start, end_row=r, end_column=end)
            for c in range(start, end + 1):
                sheet.cell(row=r, column=c).fill = _BLUE
                sheet.cell(row=r, column=c).border = _BOX
            cell.font = _WHITE_BOLD
            cell.alignment = Alignment(horizontal='center', vertical='center')
        r += 1
        for c, header in enumerate(HEADERS, start=1):
            cell = sheet.cell(row=r, column=c, value=header)
            cell.font, cell.fill, cell.border = _WHITE_BOLD, _BLUE, _BOX
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        r += 1
        for n, item in enumerate(items, start=1):
            values = {**item['cells'], 'SR.NO': n}
            for c, header in enumerate(HEADERS, start=1):
                value = values[header]
                if header in MONEY and value is not None:
                    value = float(value)
                cell = sheet.cell(row=r, column=c, value=value)
                cell.font, cell.border = _BODY, _BOX
                if header in MONEY:
                    cell.number_format = _MONEY_FORMAT
                    cell.alignment = Alignment(horizontal='right')
                elif header in ('SR.NO', 'UNIT', 'BRANCH', 'DEBIT SINCE', 'DEBIT FROM'):
                    cell.alignment = Alignment(horizontal='center')
            r += 1
        r += 1  # a blank row between blocks
    sheet.freeze_panes = None
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
