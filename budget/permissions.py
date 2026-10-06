"""Who may see budget approvals, decide them, and change the auto-approval setting.

Deciding needs BOTH the key and being the item's current stage user today
(`flow.may_act_on`): the key opens the desk, the stage decides whose item it is.
The stage also scopes a user to a company — a workflow names one company.
"""
from core.permissions import HasKey, effective_keys

#: The approval desk: see drafts, decide your own items.
APPROVAL_KEY = 'Budget_Approval'
#: Change auto-approval (on/off, hours, exempt users).
SETTINGS_KEY = 'Budget_Settings'


def CanUseDesk():
    return HasKey(APPROVAL_KEY)


def CanChangeSettings():
    return HasKey(SETTINGS_KEY)


def has_settings_access(user):
    return SETTINGS_KEY in effective_keys(user)
