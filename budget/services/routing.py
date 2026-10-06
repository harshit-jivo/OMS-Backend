"""Which approval route a gated line belongs to.

By BUDGET HEAD (`OcrCode3`), the hierarchy's unit. Lines on the electricity
account get their own route, because the hierarchy adds Director Approval
after the owner for electricity — the same owner, one more stage.

    Factory, account 5100008        -> FACTORY
    Factory, account 5680011        -> FACTORY_ELECTRICITY
    Sales RE                        -> SALES_RE
    R & D                           -> R_D

A line with no budget code never reaches here: the intake (like JSAP's) does
not take it in, and SAP keeps the draft unpostable until its creator adds one.
"""
import re

#: Accounts routed as electricity (owner, then Director Approval).
ELECTRICITY_ACCOUNTS = frozenset({'5680011'})


def head_key(budget_code):
    """`Sales RE` -> `SALES_RE`, `R & D` -> `R_D`: a stable, query-safe name for a head."""
    return re.sub(r'[^A-Z0-9]+', '_', (budget_code or '').strip().upper()).strip('_')


def route_for(budget_code, acct_code):
    """The route of one line, or None when it has no budget code."""
    head = head_key(budget_code)
    if not head:
        return None
    return f'{head}_ELECTRICITY' if (acct_code or '').strip() in ELECTRICITY_ACCOUNTS else head
