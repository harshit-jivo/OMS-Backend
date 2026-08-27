"""Centralized page-access rules for the Document Tracker.

Access is decided in ONE place — `tracker_pages_for(user)` — driven entirely by
the user's role. Every view (and, mirrored, the frontend) reads from it, so to
change who sees what you change a user's role, never page code.

Four tracker sub-roles:

  * tracker_admin  -> ALL tracker pages (incl. Invoice Entry + Stuck Alerts + AP)
  * tracker_entry  -> Invoice Entry + My Stage Queue
  * tracker_user   -> My Stage Queue
  * tracker_ap     -> AP Invoice Entry (vendor invoices copied from a GRPO)

Stuck Alerts is admin-only. Access is decided by the tracker sub-role and
nothing else: `is_superuser` and the OMS 'admin' role are NOT special-cased, so
a superuser whose role is 'admin' sees no tracker pages at all. To give someone
tracker access, set their role — granting superuser does nothing here.
(Corrected 2026-08-26; this paragraph previously claimed the opposite, which
`tracker_pages_for` below has never done.)

"Their role" means any role they hold — the primary `role` FK OR one of
`extra_roles` — and the pages of all of them are unioned. `extra_roles` was
ignored here until 2026-08-27, which meant a user had to give up their primary
role to run the tracker. See `tracker_pages_for`.
"""
from rest_framework.permissions import BasePermission

# Page keys — must match the frontend config in src/config/pageAccess.ts.
PAGE_ENTRY = 'Tracker_Entry'
PAGE_QUEUE = 'Tracker_Queue'
PAGE_ALERTS = 'Tracker_Alerts'
PAGE_REPORTS = 'Tracker_Reports'
PAGE_ADMIN = 'Tracker_Admin'
PAGE_INVOICES = 'Tracker_Invoices'  # admin master list of every invoice
PAGE_AP = 'Ap_Invoice_Entry'        # A/P vendor invoice entry (copy from GRPO)

ALL_TRACKER_PAGES = {PAGE_ENTRY, PAGE_QUEUE, PAGE_ALERTS, PAGE_REPORTS,
                     PAGE_ADMIN, PAGE_INVOICES, PAGE_AP}

# The single source of truth: tracker sub-role -> visible pages.
# Stuck Alerts and the all-invoices list are admin-only.
ROLE_PAGE_MAP = {
    'tracker_admin': {PAGE_ENTRY, PAGE_QUEUE, PAGE_ALERTS, PAGE_REPORTS,
                      PAGE_ADMIN, PAGE_INVOICES, PAGE_AP},
    'tracker_entry': {PAGE_ENTRY, PAGE_QUEUE},
    'tracker_user': {PAGE_QUEUE},
    # AP data entry is a distinct job from document tracking: these users post
    # vendor invoices into SAP and have no reason to see the tracker queues.
    'tracker_ap': {PAGE_AP},
}


def tracker_pages_for(user):
    """The set of tracker page keys this user may access.

    Purely role-driven — tracker pages are for the four tracker sub-roles only.
    The OMS 'admin' role and superusers are NOT special-cased here; that is
    deliberate and documented in the module docstring above.

    Consults EVERY role the user holds, primary and `extra_roles`, and unions
    the pages. It previously read the `role` FK alone, which contradicted the
    rule stated in `users/models.py`:

        "Anything resolving 'does this user hold role X' must check BOTH."

    The consequence was a real access bug, not a theoretical one: `role` is a
    single FK, so a manager who also runs the tracker had to choose between
    `manager` and `tracker_admin`. `extra_roles` exists precisely to remove that
    choice — and here it did nothing, so the user got no tracker access at all
    while every other module honoured the grant.

    Unioning rather than first-match-wins is what makes the multi-role case
    behave: a `tracker_entry` who is also `tracker_ap` gets all three pages,
    which is the only reading under which holding two roles is not worse than
    holding one.
    """
    if not (user and user.is_authenticated):
        return set()

    # `core.permissions.role_names` is the project-wide resolver (primary FK
    # plus extra_roles, lowercased). Imported here rather than at module level
    # to keep this module importable during migrations, when the M2M table may
    # not exist yet.
    from core.permissions import role_names

    pages = set()
    for name in role_names(user):
        pages |= ROLE_PAGE_MAP.get(name, set())
    return pages


class _BaseTrackerPermission(BasePermission):
    """Grants access when the user holds `required_page`. If `required_page`
    is None, any tracker page (i.e. being a tracker user) suffices."""
    required_page = None
    message = 'You do not have access to this tracker page.'

    def has_permission(self, request, view):
        pages = tracker_pages_for(request.user)
        if self.required_page is None:
            return bool(pages)
        return self.required_page in pages


class IsTrackerUser(_BaseTrackerPermission):
    required_page = None


class IsTrackerEntry(_BaseTrackerPermission):
    required_page = PAGE_ENTRY


class IsTrackerAlerts(_BaseTrackerPermission):
    required_page = PAGE_ALERTS


class IsTrackerReports(_BaseTrackerPermission):
    required_page = PAGE_REPORTS


class IsTrackerAdmin(_BaseTrackerPermission):
    """Manage tracker configuration (stages, lookups, stage assignments)."""
    required_page = PAGE_ADMIN
    message = 'Tracker administration is restricted to tracker admins.'


class IsTrackerAP(_BaseTrackerPermission):
    """Post A/P (vendor) invoices into SAP — tracker_ap and tracker_admin."""
    required_page = PAGE_AP
    message = 'AP invoice entry is restricted to AP users.'
