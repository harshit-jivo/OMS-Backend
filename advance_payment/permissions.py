"""Who may read the advance-payment lookups.

ONE KEY, AND NO PER-COMPANY SCOPING — the same conclusion `production` reached
and for the same reason. OMS has no user->company mapping that scopes
documents: `users.User.company` is an organisational master that
`core/companies.py` says explicitly does not scope, and
`payments.user_companies()` returns every company to everyone. Inventing a
scope here would be inventing it, not enforcing it.

So the key opens the lookups for every configured company. When the advance
REQUEST itself lands, its approval will be scoped the way PRDO's is — by the
workflow stage, because a stage belongs to a workflow that names one company,
and naming a user on that stage is exactly "this person approves for this
company".

`HasKey` is parameterised, so views instantiate it in `get_permissions()`
rather than listing it in `permission_classes`; DRF calls `permission()` on
every entry of that list and a `HasKey` instance is not callable.
"""
from core.permissions import HasKey, effective_keys

#: Read the pickers an advance payment is raised from.
VIEW_KEY = 'Advance_Payment'


def has_view_access(user):
    return VIEW_KEY in effective_keys(user)


class CanViewLookups(HasKey):
    def __init__(self):
        super().__init__(VIEW_KEY)
