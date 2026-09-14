"""The canonical company domain for NEW apps.

Why this exists
---------------
The document-level company enum — OIL / BEVERAGES / MART — is currently
copy-pasted into five modules: `approvals.CATEGORY_CHOICES`,
`payments.CATEGORY_CHOICES`, `orders.Scheme.CATEGORY_CHOICES`,
`sap_sync` (twice) and `users` (twice). A new app needs the same values, and
there is no shared definition to import.

This module is that definition, for new apps only. The five existing copies
are deliberately left alone: they back live columns and migrations, and
rewriting them would be a change with a client-visible blast radius for no
functional gain. This is the same approach `core.models.TimeStampedModel` and
`core.responses` already take — standardise for the new code without touching
what works.

Which "company" this is
-----------------------
There are three different things called company in this project, and only one
of them scopes a document:

* `users.Company` — an ORGANISATIONAL master (int PK, free-text name) that
  `User.company` points at. It does not scope documents.
* `orders.Categories` — the relational master behind `User.category`
  (int PK, `varchar(255)`, no unique constraint).
* **these values** — what `ApprovalRequest.company`, `PaymentReceipt.company`
  and `Scheme.category` actually store, and what the SAP company databases
  (`HANA_COMPANY_DB*`) are keyed by.

A new app that scopes business documents wants THIS one. Note that it is
intentionally a closed string set rather than a foreign key: there is no
authoritative integer master for document company, and pointing an FK at
either of the other two would give the column a meaning it does not have.
A database CHECK over these values provides the same guarantee an FK would.
"""

OIL = 'OIL'
BEVERAGES = 'BEVERAGES'
MART = 'MART'

#: Django `choices` for a company column. Same values and labels as the five
#: existing `CATEGORY_CHOICES` copies, so a column using this is
#: wire-compatible with the rest of the API.
COMPANY_CHOICES = [
    (OIL, 'Oil'),
    (BEVERAGES, 'Beverages'),
    (MART, 'Mart'),
]

#: Just the codes, for `in` tests and for building database CHECK constraints.
COMPANY_CODES = tuple(code for code, _label in COMPANY_CHOICES)


def is_valid(code):
    """Whether `code` is a known company code. Empty/None is not valid."""
    return code in COMPANY_CODES
