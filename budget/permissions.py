"""Who may see budget approvals, decide them, and change the auto-approval setting.

Deciding needs BOTH the key and being the item's current stage user today
(`flow.may_act_on`): the key opens the desk, the stage decides whose item it is.
The stage also scopes a user to a company — a workflow names one company.
"""
from core.permissions import HasAnyKey, HasKey, effective_keys

#: The approval desk: see drafts, decide your own items.
APPROVAL_KEY = 'Budget_Approval'
#: Change auto-approval (on/off, hours, exempt users).
SETTINGS_KEY = 'Budget_Settings'
#: Every item and every approver's decisions, by month, with export. Read-only.
REPORTS_KEY = 'Budget_Reports'


def CanUseDesk():
    return HasKey(APPROVAL_KEY)


def CanReadReports():
    return HasKey(REPORTS_KEY)


def CanReadDrafts():
    """A draft, its items and attachments: from the desk, or from the reports."""
    return HasAnyKey(APPROVAL_KEY, REPORTS_KEY)


def CanChangeSettings():
    return HasKey(SETTINGS_KEY)


def has_settings_access(user):
    return SETTINGS_KEY in effective_keys(user)
