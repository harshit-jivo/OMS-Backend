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

The server re-checks this on every raise and edit, inside the transaction and
under a per-document lock, so two requests raised at once cannot both take
the last rupee.
"""
import hashlib
from collections import defaultdict
from decimal import Decimal

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
        for d in docs:
            use = used.get(_key(d['sap_doc_entry'], d.get('sap_line', 0)))
            if not use:
                continue
            left = available(kind, d['open_amount'], use)
            if d['amount'] > left:
                label = d.get('sap_doc_num') or d['sap_doc_entry']
                held = use['reserved'] + (use.get('unadjusted', use['paid']) if subtracts_paid(kind) else ZERO)
                found.append(f'{DocumentKind(kind).label} {label}: only {left} is available — {held} of its '
                             f'{d["open_amount"]} open is already held by other OMS requests.')
    return found


def annotate(company, kind, rows, *, key, open_field, drop_exhausted=True):
    """Add `oms: {reserved, paid, available, requests}` to SAP rows; drop the used-up ones.

    `key(row)` gives `(doc_entry, line)`; `open_field` names SAP's open amount.
    """
    used = usage(company, kind, [key(r) for r in rows])
    out = []
    for r in rows:
        use = used.get(_key(*key(r)))
        left = available(kind, r.get(open_field), use)
        r['oms'] = {
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
            'raised_by': (user.get_full_name() or user.get_username()) if user else '',
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
        left = available(kind, open_now, used.get(key)) if status == 'OPEN' else ZERO
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
