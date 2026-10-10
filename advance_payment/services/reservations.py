"""What OMS has RESERVED and PAID against each SAP document.

There is no separate ledger to keep in step: every request already records,
per line (`RequestDocument`), which SAP document it pays, what was open when
it was raised and how much it pays. The request's status says what that line
means now:

    in approval / returned to creator   RESERVED   (held until it finishes)
    completed                           PAID       (the money went)
    rejected / cancelled                RELEASED   (counts for nothing)

So a reservation is released the moment a request is rejected or cancelled,
and becomes a payment the moment it completes, with nothing to update.

WHAT IS STILL AVAILABLE
-----------------------
SAP's own open amount, less what OMS is holding:

  * a BILL or a ledger item: SAP's open amount already falls when OMS's
    payment posts (it is applied to the bill), so only what is RESERVED is
    taken off; taking PAID off too would count a payment twice.
  * a PO: an advance against a PO posts ON ACCOUNT, which SAP's PO figure
    never sees. So what OMS has paid comes off too, but only the part SAP
    still holds ON ACCOUNT ("unadjusted"): once accounts set the advance off
    against the bill for the goods, SAP's PO open amount has already fallen
    by those goods, and counting the advance again would hold it twice.
    Read from the payment's partner line in SAP (`sap.payments_unadjusted`);
    if SAP cannot be read, the whole payment counts — the cautious side.

  BEFORE THE MONEY GOES, the request is checked against SAP as it is now
  (`live_check`): a PO or bill amended, part-paid outside OMS, closed or
  cancelled since it was raised shows on the desk from Payment on, and Final
  cannot post a line that no longer fits.

A document with nothing available is not offered again.

WHERE OMS ALONE TRACKS (decided 2026-10-07)
-------------------------------------------
Every PO (whatever its date) and every bill CREATED in SAP on or after
`ADVANCE_PAYMENT_TRACK_FROM` (2026-10-08) is tracked by OMS alone, and SAP's
own figures are not used:

    open       = the document's total  -  what OMS has PAID against it
    available  = open                  -  what OMS requests HOLD (in approval)

A PO's "already received" (GRPOs, goods in) is never deducted: an advance is
measured against the PO amount. A new bill's paid-to-date in SAP is ignored:
from the cut-off every payment against it goes through OMS. One guard stays: a
bill's payment is applied to the bill in SAP, which refuses more than its own
balance, so the live check before posting also holds a bill to SAP's balance.

A bill created BEFORE the cut-off was paid in SAP before OMS existed, so its
real balance is SAP's: SAP's open amount (total less paid in SAP) less what
OMS requests hold. Payment history stays in OMS for all of them. The "already
paid on account" ledger notice is shown beside POs created since the cut-off.
Customer ledger items are unchanged (SAP's open less what OMS holds).

The server re-checks this on every raise and edit, inside the transaction and
under a per-document lock, so two requests raised at once cannot both take
the last rupee.
"""
import hashlib
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import connection

from advance_payment.models import (
    AdvanceRequest, DocumentKind, RequestDocument, RequestStatus, SapVoucher, VoucherStatus)

#: Statuses whose lines hold their amount.
RESERVING = (RequestStatus.IN_APPROVAL, RequestStatus.RETURNED)
#: Statuses whose lines have paid it.
PAID = (RequestStatus.COMPLETED,)

ZERO = Decimal('0')


def _key(entry, line=0):
    return int(entry), int(line or 0)


#: The kinds the cut-off applies to. Customer ledger items are always tracked.
CUT_OFF_KINDS = (DocumentKind.PO, DocumentKind.BILL)


def track_from():
    """The first SAP creation date OMS tracks (`ADVANCE_PAYMENT_TRACK_FROM`)."""
    value = getattr(settings, 'ADVANCE_PAYMENT_TRACK_FROM', '') or '2026-10-08'
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def since_cut_off(created_on):
    """Whether a document was created in SAP on or after the cut-off. Unknown: yes (the cautious side)."""
    if not created_on:
        return True
    when = created_on if isinstance(created_on, date) else date.fromisoformat(str(created_on)[:10])
    return when >= track_from()


def tracked(kind, created_on):
    """Whether OMS ALONE tracks this document: open = its total less what OMS paid.

    Every PO (goods received never count against an advance), whatever its
    date, and a bill created in SAP on or after the cut-off. An older bill (or
    one whose creation date is unknown) and a customer ledger item start from
    SAP's own open amount less what OMS holds (`available`).
    """
    if kind == DocumentKind.PO:
        return True
    if kind == DocumentKind.BILL:
        # An older bill was paid in SAP before OMS: its real balance is SAP's.
        # Unknown creation date: SAP's balance too (the side that cannot overpay).
        return bool(created_on) and since_cut_off(created_on)
    return False


def sap_facts(company, kind, entries):
    """`{doc_entry: {created_on, doc_total}}` for POs / bills, read from SAP. `{}` if SAP cannot be read."""
    from advance_payment.services import sap as sap_service

    if kind not in CUT_OFF_KINDS or not entries:
        return {}
    try:
        live = sap_service.live_documents(company, kind, entries)
    except sap_service.SapUnavailable:
        return {}
    return {e: {'created_on': v.get('created_on'), 'doc_total': v.get('doc_total')} for e, v in live.items()}


def created_dates(company, kind, entries):
    """`{doc_entry: created_on}` (see `sap_facts`)."""
    return {e: f['created_on'] for e, f in sap_facts(company, kind, entries).items()}


def oms_left(total, use):
    """A tracked document: its total less what OMS has paid and what OMS requests hold."""
    use = use or {}
    left = Decimal(str(total or 0)) - use.get('paid', ZERO) - use.get('reserved', ZERO)
    return max(left, ZERO)


def subtracts_paid(kind):
    """Whether OMS's own payments still come off SAP's open amount (a PO's do)."""
    return kind == DocumentKind.PO


def usage(company, kind, keys, *, exclude_request=None):
    """`{(doc_entry, line): {'reserved', 'paid', 'requests'}}` for those keys.

    `requests` counts every request that has touched the document, whatever
    its status. `exclude_request` leaves one request out (the one being edited).
    """
    keys = {_key(*k) for k in keys}
    out = defaultdict(lambda: {'reserved': ZERO, 'paid': ZERO, 'unadjusted': ZERO, 'requests': 0})
    if not keys:
        return {}
    paid_lines = []
    rows = (RequestDocument.objects
            .filter(request__company=company, kind=kind,
                    sap_doc_entry__in={entry for entry, _line in keys})
            .values_list('sap_doc_entry', 'sap_line', 'amount', 'request_id', 'request__status'))
    for entry, line, amount, request_id, status in rows:
        key = (entry, line)
        if key not in keys or request_id == exclude_request:
            continue
        use = out[key]
        use['requests'] += 1
        if status in RESERVING:
            use['reserved'] += amount
        elif status in PAID:
            use['paid'] += amount
            paid_lines.append((key, request_id, amount))
    if subtracts_paid(kind) and paid_lines:
        for (key, _request_id, _amount), still in zip(paid_lines, unadjusted(company, paid_lines)):
            out[key]['unadjusted'] += still
    return dict(out)


def unadjusted(company, paid_lines):
    """For each `(key, request_id, amount)`: how much of that line SAP still holds on account.

    A request's payment may cover several POs, so its on-account balance is
    shared between its lines in proportion to what each paid. A payment SAP
    cannot be asked about counts in full.
    """
    from advance_payment.services import sap as sap_service

    ids = {request_id for _key_, request_id, _amount in paid_lines}
    vouchers = dict(SapVoucher.objects
                    .filter(request_id__in=ids, status=VoucherStatus.POSTED, replaced_by__isnull=True)
                    .values_list('request_id', 'sap_doc_entry'))
    totals = dict(AdvanceRequest.objects.filter(pk__in=ids).values_list('pk', 'amount'))
    try:
        balances = sap_service.payments_unadjusted(company, [e for e in vouchers.values() if e])
    except sap_service.SapUnavailable:
        balances = {}
    out = []
    for _key_, request_id, amount in paid_lines:
        entry, total = vouchers.get(request_id), totals.get(request_id)
        if not entry or entry not in balances or not total:
            out.append(amount)  # unknown: the cautious side
            continue
        share = (balances[entry] / Decimal(total)) * amount
        out.append(min(amount, share.quantize(Decimal('0.01'))))
    return out


def available(kind, open_amount, use):
    """What is left to pay against a document whose SAP open amount is `open_amount`."""
    use = use or {}
    left = Decimal(str(open_amount or 0)) - use.get('reserved', ZERO)
    if subtracts_paid(kind):
        # Only what SAP still holds on account: an adjusted advance is in the
        # PO's open amount already.
        left -= use.get('unadjusted', use.get('paid', ZERO))
    return max(left, ZERO)


def lock(company, kind, keys):
    """Serialise raises against the same documents until the transaction ends.

    A transaction-scoped Postgres advisory lock per document, taken in a fixed
    order so two requests on overlapping documents cannot deadlock. No-op on
    any other database (the test runner's).
    """
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        for entry, line in sorted({_key(*k) for k in keys}):
            digest = hashlib.blake2b(f'ap|{company}|{kind}|{entry}|{line}'.encode(), digest_size=8).digest()
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [int.from_bytes(digest, 'big', signed=True)])


def problems(company, documents, *, exclude_request=None):
    """Every line that asks for more than is still available. `[]` when all fit.

    `documents` are `clean()`'s line dicts; their `open_amount` is SAP's, as
    the creator saw it.
    """
    found = []
    by_kind = defaultdict(list)
    for doc in documents:
        by_kind[doc['kind']].append(doc)
    for kind, docs in by_kind.items():
        keys = [(d['sap_doc_entry'], d.get('sap_line', 0)) for d in docs]
        lock(company, kind, keys)
        used = usage(company, kind, keys, exclude_request=exclude_request)
        facts = sap_facts(company, kind, [d['sap_doc_entry'] for d in docs])
        for d in docs:
            use = used.get(_key(d['sap_doc_entry'], d.get('sap_line', 0)))
            fact = facts.get(int(d['sap_doc_entry'])) or {}
            if tracked(kind, fact.get('created_on')) and fact.get('doc_total') is not None:
                # From the cut-off OMS alone tracks it: total less paid and held.
                left = oms_left(fact['doc_total'], use)
                if d['amount'] > left:
                    label = d.get('sap_doc_num') or d['sap_doc_entry']
                    found.append(f'{DocumentKind(kind).label} {label}: only {left} is available — of its total '
                                 f'{fact["doc_total"]}, OMS has paid {(use or {}).get("paid", ZERO)} and other '
                                 f'requests hold {(use or {}).get("reserved", ZERO)}.')
                continue
            if not use:
                continue
            left = available(kind, d['open_amount'], use)
            if d['amount'] > left:
                label = d.get('sap_doc_num') or d['sap_doc_entry']
                held = use['reserved'] + (use.get('unadjusted', use['paid']) if subtracts_paid(kind) else ZERO)
                found.append(f'{DocumentKind(kind).label} {label}: only {left} is available — {held} of its '
                             f'{d["open_amount"]} open is already held by other OMS requests.')
    return found


def annotate(company, kind, rows, *, key, open_field, total_field=None, paid_field=None, drop_exhausted=True):
    """Add `oms: {tracked, reserved, paid, available, requests}` to SAP rows; drop the used-up ones.

    `key(row)` gives `(doc_entry, line)`; `open_field` names SAP's open amount.
    A row OMS alone tracks (created since the cut-off, with `total_field`) is
    REWRITTEN to OMS's figures: `open_field` = total less OMS paid, and
    `paid_field` (SAP's received / paid-to-date) = what OMS has paid.
    """
    used = usage(company, kind, [key(r) for r in rows])
    out = []
    for r in rows:
        use = used.get(_key(*key(r)))
        is_tracked = tracked(kind, r.get('created_on'))
        if is_tracked and total_field and r.get(total_field) is not None:
            paid = (use or {}).get('paid', ZERO)
            r[open_field] = str(max(Decimal(str(r[total_field])) - paid, ZERO))
            if paid_field:
                r[paid_field] = str(paid)
            left = oms_left(r[total_field], use)
        else:
            # Older (or nothing to work from): SAP's open less what OMS holds and paid.
            left = available(kind, r.get(open_field), use)
        r['oms'] = {
            # True: OMS alone tracks it (every PO; a bill created since the
            # cut-off) — open = total less OMS paid. False: SAP's open less what OMS holds.
            'tracked': is_tracked,
            'reserved': str(use['reserved']) if use else '0',
            'paid': str(use['paid']) if use else '0',
            # A PO's paid advances still on account in SAP (what it holds of `paid`).
            'unadjusted': str(use['unadjusted']) if use and subtracts_paid(kind) else '0',
            'available': str(left),
            'requests': use['requests'] if use else 0,
        }
        if drop_exhausted and use and left <= 0:
            continue
        out.append(r)
    return out


def history(company, kind, doc_entry, line=0):
    """Every OMS request that has paid or reserved against one document, newest first."""
    rows = (RequestDocument.objects
            .filter(request__company=company, kind=kind, sap_doc_entry=int(doc_entry), sap_line=int(line or 0))
            .select_related('request', 'request__created_by')
            .prefetch_related('request__vouchers')
            .order_by('-request__created_on'))
    rows = list(rows)
    results, reserved, paid = [], ZERO, ZERO
    paid_rows = [d for d in rows if d.request.status in PAID]
    still = (dict(zip((d.pk for d in paid_rows),
                      unadjusted(company, [((d.sap_doc_entry, d.sap_line), d.request_id, d.amount)
                                           for d in paid_rows])))
             if subtracts_paid(kind) and paid_rows else {})
    for d in rows:
        advance = d.request
        if advance.status in RESERVING:
            reserved += d.amount
            effect = 'RESERVED'
        elif advance.status in PAID:
            paid += d.amount
            effect = 'PAID'
        else:
            effect = 'RELEASED'
        voucher = next((v for v in advance.vouchers.all() if v.status == VoucherStatus.POSTED), None)
        user = advance.created_by
        results.append({
            'request_id': advance.pk,
            'request_no': advance.request_no,
            'status': advance.status,
            'effect': effect,
            'amount': str(d.amount),
            'open_amount': str(d.open_amount),
            'original_amount': str(d.original_amount),
            'raised_on': advance.created_on.isoformat() if advance.created_on else None,
            'raised_by': ((getattr(user, 'name', '') or '').strip() or user.get_username()) if user else '',
            'sap_payment': voucher.sap_doc_num if voucher else None,
            # A PO advance: how much SAP still holds on account (not yet set off against a bill).
            'unadjusted': str(still[d.pk]) if d.pk in still else None,
        })
    return {
        'summary': {'reserved': str(reserved), 'paid': str(paid), 'requests': len(results)},
        'results': results,
    }


def live_check(advance):
    """Each line of `advance` against SAP as it is NOW. `[{..., ok, message}]`.

    For each document: SAP's open amount when it was raised and now, its
    status, what other OMS requests hold, what is left for this request, and
    whether this request's line still fits. Raises `SapUnavailable` when SAP
    cannot be read: the caller decides what that means.
    """
    from advance_payment.services import sap as sap_service

    docs = list(advance.documents.all())
    if not docs:
        return []
    kind = docs[0].kind
    keys = [(d.sap_doc_entry, d.sap_line) for d in docs]
    if kind == DocumentKind.LEDGER:
        items = sap_service.ledger_items(advance.company, advance.partner_code)
        live = {k: {'open': v['open'], 'status': 'OPEN'} for k, v in items.items()}
    elif kind in (DocumentKind.BILL, DocumentKind.PO):
        live = {(e, 0): v for e, v in sap_service.live_documents(advance.company, kind,
                                                                 [d.sap_doc_entry for d in docs]).items()}
    else:
        return []
    used = usage(advance.company, kind, keys, exclude_request=advance.pk)
    out = []
    for d in docs:
        key = _key(d.sap_doc_entry, d.sap_line)
        now = live.get(key)
        status = now['status'] if now else 'GONE'
        open_now = Decimal(str(now['open'])) if now else ZERO
        # From the cut-off: the total less what OMS paid and others hold — and a
        # bill no more than SAP's own balance, which SAP itself would refuse to
        # exceed. Before it: SAP's open amount less what OMS holds and paid.
        created_on = (now or {}).get('created_on')
        if status != 'OPEN':
            left = ZERO
        elif tracked(kind, created_on) and now.get('doc_total') is not None:
            left = oms_left(now['doc_total'], used.get(key))
            if kind == DocumentKind.BILL:
                left = min(left, open_now)
        else:
            left = available(kind, open_now, used.get(key))
        label = f'{DocumentKind(kind).label} {d.sap_doc_num or d.sap_doc_entry}'
        if status == 'GONE':
            message = (f'{label} is no longer open on the customer’s account in SAP.' if kind == DocumentKind.LEDGER
                       else f'{label} no longer exists in SAP.')
        elif status != 'OPEN':
            message = f'{label} is {status.lower()} in SAP.'
        elif d.amount > left:
            message = (f'{label}: this request pays {d.amount}, but only {left} is left — SAP shows '
                       f'{open_now} open now (it was {d.open_amount} when raised).')
        else:
            message = ''
        out.append({
            'document_id': d.pk,
            'kind': kind,
            'sap_doc_entry': d.sap_doc_entry,
            'sap_line': d.sap_line,
            'sap_doc_num': d.sap_doc_num,
            'status': status,
            'open_when_raised': str(d.open_amount),
            'open_now': str(open_now),
            'held_by_others': str(open_now - left) if status == 'OPEN' else '0',
            'available_now': str(left),
            'amount': str(d.amount),
            'changed': status != 'OPEN' or open_now != d.open_amount,
            'ok': not message,
            'message': message,
        })
    return out
