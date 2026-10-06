"""The budget hierarchy as workflows: one per budget head per company.

Agreed with the business on 2026-10-05/06 (`Budget_Approval_Hierarchy.xlsx`,
`docs/Approvals/BUDGET_DESIGN.md` §3). Only the rows budget approval can meet:
every gated SAP line is an indirect expense, so the Fixed Assets, RM & PM,
oil-purchase and import rows (Payments only) and salary (`Sal CF`, which SAP's
intake never sends) do not appear.

    <HEAD>               the head's owner                    (Director after, for NPD)
    <HEAD>_ELECTRICITY   the head's owner, then the Director (the electricity rule)

R & D and OTE have no owner: the Director alone. Usernames are the ones on the
test server; change people on the Workflows page, not here — this is what
`manage.py seed_budget_workflows` writes the first time.
"""
from budget.services.routing import head_key

DIRECTOR = 'Gurpreet Vg'
OWNER_STAGE = 'Budget Owner Approval'
DIRECTOR_STAGE = 'Director Approval'

#: {company: {budget code as SAP has it: owner username, or None for "the Director alone"}}
OWNERS = {
    'OIL': {
        'BackOff': 'nirmal', 'Sales': 'Raju Vg', 'Sales RE': 'Raju Vg',
        'Del Bkhp': 'bhupinder', 'Del Mayp': 'bhupinder', 'Factory': 'gagan', 'FACT_COM': 'gagan',
        'Interest': 'avtar', 'Med MKT': 'karanpreet', 'R & D': None, 'OTE': None, 'Transprt': 'paramdeep',
        'NPD1': 'avtar', 'NPD2': 'avtar', 'NPD3': 'avtar',
    },
    'BEVERAGES': {
        'BackOff': 'nirmal', 'Sales': 'karanpreet', 'Sales RE': 'karanpreet',
        'Del Bkhp': 'bhupinder', 'Del Mayp': 'bhupinder', 'Factory': 'arvinder', 'FACT_COM': 'arvinder',
        'Interest': 'avtar', 'Med MKT': 'karanpreet', 'OTE': None, 'Transprt': 'paramdeep',
        'NPD1': 'avtar', 'NPD2': 'avtar', 'NPD3': 'avtar',
    },
}

#: Heads whose owner is followed by Director Approval on every line.
DIRECTOR_AFTER = frozenset({'NPD1', 'NPD2', 'NPD3'})

COMPANY_TAG = {'OIL': 'OIL', 'BEVERAGES': 'BEV'}


def _chain(owner, director):
    stages = [(OWNER_STAGE, owner)] if owner else []
    if director or not owner:
        stages.append((DIRECTOR_STAGE, DIRECTOR))
    return stages


def routes():
    """`[{code, name, company, route, query, stages: [(stage name, username)]}]`."""
    out = []
    for company, heads in OWNERS.items():
        for budget_code, owner in heads.items():
            head = head_key(budget_code)
            for route, director, label in ((head, budget_code in DIRECTOR_AFTER, budget_code),
                                           (f'{head}_ELECTRICITY', True, f'{budget_code} · electricity')):
                out.append({
                    'code': f'BUD_{COMPANY_TAG[company]}_{route}',
                    'name': f'Budget · {company.title()} · {label}',
                    'company': company,
                    'route': route,
                    'query': (f'SELECT id FROM "budget"."budget_item" '
                              f"WHERE company = '{company}' AND route = '{route}'"),
                    'stages': _chain(owner, director),
                })
    return out
