"""Build SAP Service Layer payloads for payments and deposits.

Follows the conventions in sap_sync/services/sync_service.py:850-878 — flat
dict, ISO date strings, optional keys appended conditionally rather than sent
as null — but quantises money to 2dp instead of passing raw floats through
`_to_float` (sync_service.py:167), which is a genuine drift risk on currency.
"""
from decimal import ROUND_HALF_UP, Decimal


def money(value):
    """2dp Decimal -> float for JSON. Quantised BEFORE conversion so the float
    can never carry more precision than the amount actually has."""
    if value is None:
        return 0.0
    quantised = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return float(quantised)


def _iso(value):
    return value.isoformat() if value else None


def build_incoming_payment(receipt, *, bank_accounts, bpl_id=None):
    """SAP IncomingPayments payload for one receipt.

    `bank_accounts` maps method -> sap_gl_account for this company, resolved by
    the caller so this stays a pure function. `bpl_id` is the SAP branch, from
    SapCompanyMap.default_bpl_id — omitted entirely when not configured.

    NO user-defined fields are sent. `U_OMS_REF` / `U_OMS_IDEM` were removed
    because they do not exist on ORCT, and SAP rejects a payload carrying any
    property it does not recognise. The permanent OMS<->SAP link is the
    DocEntry SAP returns, stored on payment_receipt.sap_doc_entry.

    Field mapping is NOT uniform across tender types:
        Cash   -> CashAccount     / CashSum
        UPI    -> TransferAccount / TransferSum (+ TransferDate, TransferReference)
        Cheque -> CheckAccount    + PaymentChecks[]
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
        'Remarks': (receipt.remarks or f'OMS {receipt.receipt_no}')[:254],
    }
    # Branch. Only sent when configured — an empty BPLID is itself an error.
    if bpl_id is not None:
        payload['BPLID'] = bpl_id

    cash_total = Decimal('0')
    transfer_total = Decimal('0')
    checks = []
    transfer_ref = ''
    transfer_date = None

    for entry in receipt.methods.all():
        if entry.method == 'CASH':
            cash_total += entry.amount
        elif entry.method == 'UPI':
            transfer_total += entry.amount
            transfer_ref = transfer_ref or entry.upi_reference
            transfer_date = transfer_date or receipt.payment_date
        elif entry.method == 'CHEQUE':
            check = {
                'CheckNumber': int(entry.cheque_number) if str(
                    entry.cheque_number).isdigit() else 0,
                'BankCode': (entry.bank_name or '')[:20],
                'CheckSum': money(entry.amount),
                'DueDate': _iso(entry.cheque_date),
            }
            account = bank_accounts.get('CHEQUE')
            if account:
                check['CheckAccount'] = account
            checks.append(check)

    if cash_total > 0:
        account = bank_accounts.get('CASH')
        if account:
            payload['CashAccount'] = account
        payload['CashSum'] = money(cash_total)

    if transfer_total > 0:
        account = bank_accounts.get('UPI')
        if account:
            payload['TransferAccount'] = account
        payload['TransferSum'] = money(transfer_total)
        payload['TransferDate'] = _iso(transfer_date or receipt.payment_date)
        if transfer_ref:
            payload['TransferReference'] = transfer_ref[:50]

    if checks:
        payload['PaymentChecks'] = checks

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


def build_deposit(deposit, *, bpl_id=None):
    """SAP Deposits payload.

    No user-defined fields — see build_incoming_payment. The link back to OMS is
    the DocEntry SAP returns, stored on bank_deposit.sap_doc_entry.

    Cheque deposits reference cheques SAP already holds, by CheckKey — which is
    only known once the underlying receipt has posted. The worker refuses to
    build a cheque deposit whose lines lack it.
    """
    type_map = {'CASH': 'dt_Cash', 'CHEQUE': 'dt_Check', 'MIXED': 'dt_Check'}
    payload = {
        'DepositType': type_map.get(deposit.deposit_type, 'dt_Cash'),
        'DepositDate': _iso(deposit.deposit_date),
        'BankAccount': deposit.bank_account.sap_gl_account,
        'DepositCurrency': deposit.currency or 'INR',
        'BankChargeAmount': money(deposit.bank_charge),
        'Remarks': (deposit.remarks or f'OMS {deposit.deposit_no}')[:254],
    }
    if bpl_id is not None:
        payload['BPLID'] = bpl_id
    if deposit.slip_number:
        payload['Reference'] = deposit.slip_number[:50]

    check_keys = [
        {'CheckKey': entry.sap_check_key}
        for line in deposit.lines.select_related('receipt').all()
        for entry in line.receipt.methods.all()
        if entry.method == 'CHEQUE' and entry.sap_check_key
    ]
    if check_keys:
        payload['DepositChecks'] = check_keys
    else:
        payload['TotalLC'] = money(deposit.deposit_amount)

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
