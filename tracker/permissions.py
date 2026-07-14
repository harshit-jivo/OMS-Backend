"""Centralized page-access rules for the Document Tracker.

Access is decided in ONE place — `tracker_pages_for(user)` — driven entirely by
the user's role. Every view (and, mirrored, the frontend) reads from it, so to
change who sees what you change a user's role, never page code.

Three tracker sub-roles:

  * tracker_admin  -> all tracker pages EXCEPT Invoice Entry
  * tracker_entry  -> Invoice Entry + My Stage Queue + Stuck Alerts
  * tracker_user   -> My Stage Queue + Stuck Alerts

Superusers and the OMS 'admin' role see every tracker page. Non-tracker OMS
users see none of them.
"""
from rest_framework.permissions import BasePermission

# Page keys — must match the frontend config in src/config/pageAccess.ts.
PAGE_ENTRY = 'Tracker_Entry'
PAGE_QUEUE = 'Tracker_Queue'
PAGE_ALERTS = 'Tracker_Alerts'
PAGE_REPORTS = 'Tracker_Reports'
PAGE_ADMIN = 'Tracker_Admin'

ALL_TRACKER_PAGES = {PAGE_ENTRY, PAGE_QUEUE, PAGE_ALERTS, PAGE_REPORTS, PAGE_ADMIN}

# The single source of truth: tracker sub-role -> visible pages.
ROLE_PAGE_MAP = {
    'tracker_admin': {PAGE_QUEUE, PAGE_ALERTS, PAGE_REPORTS, PAGE_ADMIN},
    'tracker_entry': {PAGE_ENTRY, PAGE_QUEUE, PAGE_ALERTS},
    'tracker_user': {PAGE_QUEUE, PAGE_ALERTS},
}


def _role_name(user):
    role = getattr(user, 'role', None)
    return (getattr(role, 'name', '') or '').strip().lower()


def tracker_pages_for(user):
    """The set of tracker page keys this user may access."""
    if not (user and user.is_authenticated):
        return set()
    if user.is_superuser or _role_name(user) == 'admin':
        return set(ALL_TRACKER_PAGES)
    return set(ROLE_PAGE_MAP.get(_role_name(user), set()))


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
