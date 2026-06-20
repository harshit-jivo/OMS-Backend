"""Map a request path / model to a human-friendly admin page name."""
import re

# Checked top to bottom; first match wins. Order matters (more specific first).
_PATH_RULES = [
    ('page-permissions', 'Permissions'),
    ('assign-parties', 'Party Assignment'),
    ('remove-party', 'Party Assignment'),
    ('party-product', 'Party Product Assignment'),
    ('bulk-party', 'Party Product Assignment'),
    ('/sap/sync', 'SAP Sync'),
    ('create-scheme', 'Add Scheme'),
    ('flow-config', 'Order Flow Settings'),
    ('users/create', 'App User'),
]

# Matches the user create/update/delete endpoints: /api/auth/users/<id>/ and
# /api/auth/users/<id>/delete/  (but NOT /users/<id>/parties/ etc.)
_USER_DETAIL_RE = re.compile(r'/auth/users/\d+/(delete/)?$')

# Fallback when a model change happens on a path we did not recognise.
_MODEL_PAGE = {
    'users.User': 'App User',
    'users.UserPartyAssignment': 'Party Assignment',
    'users.PartyProductAssignment': 'Party Product Assignment',
    'users.SchemeProduct': 'Add Scheme',
    'sap_sync.SyncLog': 'SAP Sync',
}


def resolve_page(path):
    """Return the admin page name for a path, or '' if it is not an admin page."""
    p = path or ''
    for needle, label in _PATH_RULES:
        if needle in p:
            return label
    if _USER_DETAIL_RE.search(p):
        return 'App User'
    return ''


def page_for_model(model_cls):
    return _MODEL_PAGE.get(model_cls._meta.label, '')
