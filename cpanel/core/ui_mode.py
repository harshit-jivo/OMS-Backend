"""Which look a viewer gets: the redesigned panel, or the one that was live before.

Why this exists
---------------
The redesign replaced the shell (core/base.html) and three pages outright. Some
people want the new look, some want the old one, so both ship side by side and
each viewer picks for themselves from the profile menu.

How the choice is stored
------------------------
In the session, not the database. That means no migration, it survives a logout
(the session cookie outlives it) and it is per browser - which is what a "let me
try the new look" switch should be. Nothing about a person's data or permissions
changes with it; it only decides which template renders.

The default is OLD on purpose: nobody's screen changes until they opt in.
"""

SESSION_KEY = 'ui_mode'
NEW = 'new'
OLD = 'old'
DEFAULT = OLD

# Shell used by every page. Templates say `{% extends base_template %}`, and the
# context processor below fills it in, so one choice re-skins the whole panel.
BASE_NEW = 'core/base.html'
BASE_OLD = 'core/base_legacy.html'


def get_mode(request):
    """Always the new look inside OMS: OMS shows the Control Panel in the
    redesigned look only (C_Panel let each viewer pick; OMS has chosen)."""
    return NEW


def is_new(request):
    return get_mode(request) == NEW


def set_mode(request, mode):
    """Store the choice. Returns the mode actually stored."""
    mode = NEW if mode == NEW else OLD
    request.session[SESSION_KEY] = mode
    return mode


def pick(request, new_template, old_template):
    """The template for this viewer. Used by the few views that have two versions."""
    return new_template if is_new(request) else old_template


def ui_mode(request):
    """Context processor: every template gets `base_template` and `ui_mode`."""
    mode = get_mode(request)
    return {
        'ui_mode': mode,
        'ui_is_new': mode == NEW,
        'base_template': BASE_NEW if mode == NEW else BASE_OLD,
    }
