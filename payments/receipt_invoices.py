"""Resolve the invoice rows shown in the Payment Receipt PDF.

The query/service layer for the PDF's "Paid Invoices" section: it answers, for
one receipt, which invoices were paid and — for each — the invoice's own number,
date and TOTAL, alongside the amount this receipt applied to it.

Kept out of ``receipt_pdf`` on purpose. That module renders; deciding where a
value comes from and what to do when SAP cannot answer is not rendering.

Two rules the whole module exists to honour:

* **Invoice Total is never derived from Amount Applied.** They answer different
  questions and are routinely different — a partial payment against a ₹50,000
  invoice applies ₹30,000 and the receipt must say so. `DocTotal` comes from
  SAP's OINV, or from the snapshot OMS took when the user picked the invoice,
  or it is left blank. It is never guessed.
* **Nothing is invented.** A field OMS cannot establish is returned as None and
  the PDF omits it, while still showing the invoice number and amount applied.

Both values come from ONE batched SAP read for the whole receipt, never one
query per allocation.
"""
from __future__ import annotations

import logging

from . import hana_queries

logger = logging.getLogger(__name__)


def invoice_rows_for(receipt, *, allocations=None):
    """The Paid Invoices rows for one receipt, most authoritative first.

    Returns a list of dicts:

        {
            'invoice_no':     str,            # SAP DocNum, or DocEntry
            'invoice_date':   date | None,    # OINV.DocDate
            'invoice_total':  Decimal | None, # OINV.DocTotal — NOT the amount applied
            'amount_applied': Decimal,        # what THIS receipt applied
            'currency':       str,
        }

    Empty for an advance: an advance settles nothing, so it has no allocations
    and the PDF must not show an invoice row for it.
    """
    if allocations is None:
        allocations = list(receipt.allocations.all())
    if not allocations:
        return []

    currency = receipt.currency or 'INR'

    # ONE read for every invoice on the receipt. Failure returns {} and each
    # row falls back to the OMS snapshot below.
    details = hana_queries.fetch_invoice_details(
        company=receipt.company,
        doc_entries=[a.sap_doc_entry for a in allocations if a.sap_doc_entry],
    )

    rows = []
    for allocation in allocations:
        sap = details.get(allocation.sap_doc_entry) or {}

        # Invoice NUMBER: SAP's DocNum is what a person reads on the invoice.
        # Falls back to the OMS snapshot, then to DocEntry — never to the
        # payment's own DocEntry, which is a different document entirely.
        invoice_no = (
            sap.get('doc_num')
            or allocation.sap_doc_num
            or allocation.sap_doc_entry
        )

        # DATE: SAP first, then whatever was snapshotted at selection. None
        # when neither knows it — the PDF then leaves the cell empty.
        invoice_date = sap.get('doc_date') or allocation.invoice_date

        # TOTAL: SAP's DocTotal, else the snapshot. A zero snapshot means "not
        # captured", not "a zero-value invoice", so it is treated as unknown.
        invoice_total = sap.get('doc_total')
        if invoice_total is None and allocation.invoice_total:
            invoice_total = allocation.invoice_total

        rows.append({
            'invoice_no': str(invoice_no or ''),
            'invoice_date': invoice_date,
            'invoice_total': invoice_total,
            'amount_applied': allocation.amount_applied,
            'currency': sap.get('currency') or currency,
        })

    return rows
