"""Build SAP Service Layer payloads for payments and deposits.

Follows the conventions in sap_sync/services/sync_service.py:850-878 — flat
dict, ISO date strings, optional keys appended conditionally rather than sent
as null — but quantises money to 2dp instead of passing raw floats through
`_to_float` (sync_service.py:167), which is a genuine drift risk on currency.
"""
from decimal import ROUND_HALF_UP, Decimal

# Tenders that reach SAP as a bank transfer (TransferSum / TransferAccount).
#
# CHEQUE is here on the strength of real SAP records, not convenience. All 66
# customer cheque receipts in JIVO_BEVERAGES_HANADB post with TrsfrAcct/TrsfrSum
# and leave CheckAcct NULL and CheckSum 0; RCT1 (SAP's cheque-lines table) has
# zero rows in every company database. The earlier PaymentChecks attempt failed
# with "-2028 No matching records found" because that path was never configured.
# See docs/CHEQUE_RECEIPT_SAP_EVIDENCE.md for the ORCT/JDT1 evidence.
#
# Accounting consequence: a cheque receipt debits the BANK directly
# (DR 1104106 / CR 1101001), exactly like UPI — NOT the cash clearing account.
TRANSFER_METHODS = ('UPI', 'CHEQUE')

# Maximum length of ORCT.Comments.
REMARKS_MAX = 254


def money(value):
    """2dp Decimal -> float for JSON. Quantised BEFORE conversion so the float
    can never carry more precision than the amount actually has."""
    if value is None:
        return 0.0
    quantised = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(quantised)


def _iso(value):
    return value.isoformat() if value else None


def build_cheque_remarks(receipt, cheque_entries):
    """SAP Remarks for a cheque receipt.

        CHEQUE | RCP-OIL-2026-27-000123 | CHQ 252525 | HDFC | 2026-08-06

    Cheque detail lives ONLY here. SAP is not told the cheque number, the
    payer's bank or the cheque date in any structured field, because the
    company's real cheque receipts carry none — they are plain transfers whose
    Comments identify the cheque. This string is therefore the whole SAP-side
    record of it, and the only way Finance can search SAP for an OMS receipt.

    User remarks are APPENDED. When the result exceeds SAP's 254 characters the
    user's text is clipped and the OMS identifier is always kept whole — losing
    the receipt number would break the reconciliation this format exists for.
    """
    parts = ['CHEQUE', receipt.receipt_no]
    for entry in cheque_entries:
        if entry.cheque_number:
            parts.append(f'CHQ {entry.cheque_number}')
        if entry.bank_name:
            parts.append(entry.bank_name)
        if entry.cheque_date:
            parts.append(_iso(entry.cheque_date))

    base = ' | '.join(str(p) for p in parts if p)
    user = (receipt.remarks or '').strip()
    if not user:
        return base[:REMARKS_MAX]

    combined = f'{base} | {user}'
    if len(combined) <= REMARKS_MAX:
        return combined
    # Clip the USER portion only. If the identifier alone already fills the
    # field there is no room for user text at all.
    room = REMARKS_MAX - len(base) - 3
    return f'{base} | {user[:room]}' if room > 0 else base[:REMARKS_MAX]


def build_incoming_payment(receipt, *, bank_accounts, bpl_id=None, series=None):
    """SAP IncomingPayments payload for one receipt.

    `bank_accounts` maps method -> sap_gl_account for this company, resolved by
    the caller so this stays a pure function. `bpl_id` is the SAP branch, from
    SapCompanyMap.default_bpl_id — omitted entirely when not configured.
    `series` is the numbering series for the POSTING month, resolved from NNM1
    by the caller (hana_queries.fetch_incoming_payment_series); omitted when not
    supplied so nothing changes for callers that do not pass it.

    NO user-defined fields are sent. `U_OMS_REF` / `U_OMS_IDEM` were removed
    because they do not exist on ORCT, and SAP rejects a payload carrying any
    property it does not recognise. The permanent OMS<->SAP link is the
    DocEntry SAP returns, stored on payment_receipt.sap_doc_entry.

    Field mapping is NOT uniform across tender types:
        Cash   -> CashAccount     / CashSum
        UPI    -> TransferAccount / TransferSum (+ TransferDate, TransferReference)
        Cheque -> TransferAccount / TransferSum, cheque detail in Remarks

    Cheque is a TRANSFER, not a PaymentChecks document — see TRANSFER_METHODS.
    """
    payload = {
        'CardCode': receipt.card_code,
        'DocDate': _iso(receipt.payment_date),
        # SAP requires a tax date; without it the document is rejected on
        # company setups that mandate one.
        'TaxDate': _iso(receipt.payment_date),
        'DocType': 'rCustomer',
        'DocCurrency': receipt.currency or 'INR',
        # Remarks is MANDATORY on this SAP configuration. The receipt number is
        # embedded so a human reading the document in SAP can trace it back,
        # even though it is not a queryable key.
        'Remarks': (receipt.remarks or f'OMS {receipt.receipt_no}')[:REMARKS_MAX],
    }
    # Branch. Only sent when configured — an empty BPLID is itself an error.
    if bpl_id is not None:
        payload['BPLID'] = bpl_id
    # Numbering series for the posting month. Company-specific: August 2026 is
    # 2514 in BEVERAGES and 2564 in TEST_OIL, so it is never hardcoded.
    if series is not None:
        payload['Series'] = int(series)

    cash_total = Decimal('0')
    transfer_total = Decimal('0')
    transfer_account = ''
    transfer_ref = ''
    transfer_date = None
    cheque_entries = []

    for entry in receipt.methods.all():
        if entry.method == 'CASH':
            cash_total += entry.amount

        elif entry.method in TRANSFER_METHODS:
            # SAP has ONE TransferAccount/TransferSum pair per document, so a
            # receipt may carry only one transfer-type method. The serializer
            # enforces one method per receipt; this guard is the backstop for
            # any path that bypasses it (a shell, a data fix, a future caller).
            #
            # It raises instead of silently picking the first account. That
            # silent pick is what produced DocEntry 20802: ₹12,00,000 of cheque
            # money posted to the UPI bank G/L, invisible until someone
            # reconciled the ledger.
            resolved = bank_accounts.get(entry.method, '')
            if transfer_account and resolved and resolved != transfer_account:
                raise ValueError(
                    f'Receipt {receipt.receipt_no} mixes transfer tenders that '
                    f'map to different SAP accounts ({transfer_account} and '
                    f'{resolved}). SAP allows one TransferAccount per payment, '
                    f'so this must be split into separate receipts.')
            transfer_total += entry.amount
            transfer_account = transfer_account or resolved
            transfer_ref = transfer_ref or entry.upi_reference
            transfer_date = transfer_date or receipt.payment_date
            if entry.method == 'CHEQUE':
                # Kept for the Remarks composer — this is the ONLY place the
                # cheque number, payer bank and cheque date reach SAP.
                cheque_entries.append(entry)

    # Cheque detail replaces the generic remark. Done after the loop so a
    # receipt mixing cheque with another tender still identifies its cheques.
    if cheque_entries:
        payload['Remarks'] = build_cheque_remarks(receipt, cheque_entries)

    if cash_total > 0:
        account = bank_accounts.get('CASH')
        if account:
            payload['CashAccount'] = account
        payload['CashSum'] = money(cash_total)

    if transfer_total > 0:
        if transfer_account:
            payload['TransferAccount'] = transfer_account
        payload['TransferSum'] = money(transfer_total)
        payload['TransferDate'] = _iso(transfer_date or receipt.payment_date)
        if transfer_ref:
            payload['TransferReference'] = transfer_ref[:50]


    # An advance / on-account receipt omits PaymentInvoices ENTIRELY. Sending
    # an empty array is a common cause of SAP rejecting the document.
    allocations = list(receipt.allocations.all())
    if allocations and not receipt.is_advance:
        payload['PaymentInvoices'] = [
            {
                'LineNum': index,
                'DocEntry': allocation.sap_doc_entry,
                'InvoiceType': 'it_Invoice' if allocation.invoice_type == 13
                else 'it_CredItnote',
                'SumApplied': money(allocation.amount_applied),
            }
            for index, allocation in enumerate(allocations)
        ]

    return payload


def build_deposit(deposit, *, bpl_id=None, series=None, amount=None,
                  source_gl=None):
    """SAP payload for banking collected CASH.

    An ACCOUNT-TYPE INCOMING PAYMENT, not an ODPS Deposit. The company has
    never used the Deposit object — ODPS holds 0 rows in all three live
    databases — while account-type Incoming Payments are how every real
    deposit is recorded. Verified against TransId 224988 in JIVO_OIL_HANADB:

        ORCT  DocEntry 21789  DocType A
              CardCode  1105001  CASH SALE   <- source G/L being emptied
              TrsfrAcct 1104107  TrsfrSum 186,250
        JDT1  DR 1104107 ICICI BANK  186,250
              CR 1105001 CASH SALE   186,250

    So: DocType 'A', CardCode = the cash-sale G/L, and the destination bank in
    the TRANSFER fields. `CashSum` is not used even though this banks cash —
    the money moves account-to-account, which SAP models as a transfer.

    `amount` is the CASH share only, computed by the caller. A cheque in the
    same deposit is deliberately excluded: it debited the bank when its
    RECEIPT posted, so re-posting it here would double-debit.
    """
    total = deposit.deposit_amount if amount is None else amount
    payload = {
        'DocType': 'A',
        'CardCode': source_gl or '',
        'DocDate': _iso(deposit.deposit_date),
        'TaxDate': _iso(deposit.deposit_date),
        'DocCurrency': deposit.currency or 'INR',
        'TransferAccount': deposit.bank_gl_account,
        'TransferSum': money(total),
        'TransferDate': _iso(deposit.deposit_date),
        'Remarks': (deposit.remarks or f'OMS {deposit.deposit_no}')[:REMARKS_MAX],
    }
    if bpl_id is not None:
        payload['BPLID'] = bpl_id
    if series is not None:
        payload['Series'] = int(series)
    if deposit.slip_number:
        payload['TransferReference'] = deposit.slip_number[:50]

    return payload


REDACT_KEYS = {'CheckNumber', 'TransferReference', 'AccounttNum', 'BankCode'}


def redact(payload):
    """Copy of a payload with bank/cheque detail masked, for the call log.

    print_sap_payload (sync_service.py:161-164) logs whole payloads at WARNING;
    doing that with cheque numbers would leak customer bank data into stdout.
    """
    if not isinstance(payload, dict):
        return payload
    out = {}
    for key, value in payload.items():
        if key in REDACT_KEYS and value:
            out[key] = '***'
        elif isinstance(value, dict):
            out[key] = redact(value)
        elif isinstance(value, list):
            out[key] = [redact(item) for item in value]
        else:
            out[key] = value
    return out
