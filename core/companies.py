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


# ---------------------------------------------------------------------------
# Company SETS, for a document that legitimately covers more than one
# ---------------------------------------------------------------------------
#
# Most documents name ONE company. A BackDate request is the exception: the
# rights being asked for are the same rights in two or three SAP databases,
# decided once by the same approvers. Splitting that into separate requests
# made one person's single decision into two or three approval chains that
# could disagree with each other.
#
# So a set of companies is stored as ONE canonical string — `OIL,MART` — and
# the fan-out happens where it belongs, at the SAP call. Canonical means
# deduplicated and in the order `COMPANY_CHOICES` declares, so a given set has
# exactly one spelling: `MART,OIL` and `OIL,MART` are the same request and
# must not be two different column values.
#
# `COMPANY_SET_SEPARATOR` is a comma because that is what the existing SAP
# workflow queries already match on (`company LIKE '%,%'`), and because the
# JSAP tables this replaced used the same convention.

COMPANY_SET_SEPARATOR = ','

#: Rank of each company in the canonical order, for sorting a chosen set.
_COMPANY_RANK = {code: i for i, code in enumerate(COMPANY_CODES)}


def canonical_company_set(value):
    """Normalise a chosen set of companies to its one canonical spelling.

    Accepts a list/tuple/set of codes, or a string that is already a
    separator-joined set. Returns `(canonical, error)` — never raises — so a
    serializer, a model and a management command can all report the same
    message in their own way.

    Case and order are forgiven; an unknown code is not. Duplicates collapse
    rather than erroring: ticking a box twice is not a mistake worth a
    message, whereas asking for a company that does not exist is.
    """
    if isinstance(value, (list, tuple, set, frozenset)):
        raw = [str(v) for v in value]
    else:
        raw = str(value or '').split(COMPANY_SET_SEPARATOR)

    picked = []
    for entry in raw:
        code = entry.strip().upper()
        if not code:
            continue
        if code not in _COMPANY_RANK:
            return '', (f'"{code}" is not a company. Use one of: '
                        f'{", ".join(COMPANY_CODES)}.')
        if code not in picked:
            picked.append(code)

    if not picked:
        return '', 'At least one company is required.'

    picked.sort(key=_COMPANY_RANK.__getitem__)
    return COMPANY_SET_SEPARATOR.join(picked), None


def company_set_values():
    """Every canonical set, for a database CHECK over a company-set column.

    Enumerated rather than pattern-matched on purpose. A regex could accept
    `MART,OIL` or `OIL,OIL`, which are spellings of a set the application
    would never write — so the database would be agreeing to values that can
    only arrive through a bug. With three companies there are seven sets, and
    listing them makes the column's domain exactly the domain.
    """
    from itertools import combinations
    sets = []
    for size in range(1, len(COMPANY_CODES) + 1):
        for combo in combinations(COMPANY_CODES, size):
            sets.append(COMPANY_SET_SEPARATOR.join(combo))
    return sets


def company_set_labels(value):
    """A canonical set as human labels — `"OIL,MART"` -> `"Oil, Mart"`."""
    labels = dict(COMPANY_CHOICES)
    codes = [c for c in str(value or '').split(COMPANY_SET_SEPARATOR) if c]
    return ', '.join(labels.get(code, code) for code in codes)
