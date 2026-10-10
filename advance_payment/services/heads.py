"""Department Heads: picked from the employee master, approved through their OMS login.

The Department Head picker lists the employee master's HODs
(`Employee.role = HOD`, loaded from JSAP). Approving needs an OMS login, and the
master is not linked to `users_user`, so each HOD is matched to a user
ROUGHLY, by what the two tables share:

    1. the same email address;
    2. the same name, spacing and punctuation ignored ("Bhupinder Singh");
    3. the distinctive words of the name: the employee "Gagan Vg" is the user
       `gagan`, "Jasbir Singh Raju" is `Raju Vg`, "Prabhu" is `prabhjot`.
       Courtesy words (Singh, Vg, Ji, Didi, ...) do not count; a word equal to
       a username outranks one found in a name, which outranks a shared
       prefix of five letters or more.

Only one best match is taken: two users tied at the top is no match, so a
request is never routed to the wrong person on a guess. An HOD with no match
is still listed, marked, and cannot be submitted.

Some people are known by a name that is not theirs at all ("Veerji" is
Gurpreet Ji): `KNOWN_LOGINS` names their login by employee code, and wins
over the guessing. Matched by code, not by the word, because "Veerji" also
appears in other people's names ("Shunty Veerji Accounts").
"""
import re

from django.contrib.auth import get_user_model
from django.db.models import Q

from advance_payment.models import Employee, EmployeeRole

#: Words in a name that say nothing about who it is.
_COURTESY = frozenset({'singh', 'kaur', 'vg', 'ji', 'didi', 'veerji', 'sir', 'mr', 'mrs', 'ms', 'dr',
                       'accounts', 'sh', 'shri', 'smt'})
#: A shared start at least this long counts as the same name ("Prabhu", "prabhjot").
_PREFIX = 5

_USERNAME, _NAME, _PART = 3, 2, 1

#: HODs whose login no name match could find: `{employee code: username}`.
KNOWN_LOGINS = {
    'TEMP0002': 'Gurpreet Vg',   # Veerji is Gurpreet Ji (the Director)
    'JWPL0018': 'gagan',         # Gagan Vg: the Approver login, not "gagan adv" (both named Gagan)
}


def _squash(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _words(text):
    return {w for w in re.split(r'[^a-z0-9]+', (text or '').lower()) if len(w) > 1 and w not in _COURTESY}


def _active_users():
    return list(get_user_model().objects.filter(is_active=True))


def _score(employee_words, user):
    """How well a user's username and name answer the employee's distinctive words."""
    by_username, by_name = _words(user.username), _words(getattr(user, 'name', ''))
    best = 0
    for word in employee_words:
        if word in by_username:
            best = max(best, _USERNAME)
        elif word in by_name:
            best = max(best, _NAME)
        elif any(len(word) >= _PREFIX and len(other) >= _PREFIX and word[:_PREFIX] == other[:_PREFIX]
                 for other in by_username | by_name):
            best = max(best, _PART)
    return best


def match_user(employee, users=None):
    """The one OMS user an employee-master entry is, or None (no match, or a tie)."""
    users = _active_users() if users is None else users
    known = KNOWN_LOGINS.get((employee.employee_code or '').upper())
    if known:
        return next((u for u in users if u.username == known), None)
    email = (getattr(employee, 'email', '') or '').strip().lower()
    if email:
        same = [u for u in users if (u.email or '').strip().lower() == email]
        if len(same) == 1:
            return same[0]
    name = _squash(employee.employee_name)
    if name:
        same = [u for u in users if name in (_squash(getattr(u, 'name', '')), _squash(u.username))]
        if len(same) == 1:
            return same[0]
    words = _words(employee.employee_name)
    if not words:
        return None
    scored = [(score, u) for u in users if (score := _score(words, u))]
    if not scored:
        return None
    top = max(score for score, _u in scored)
    best = [u for score, u in scored if score == top]
    return best[0] if len(best) == 1 else None


def heads(search=''):
    """The HODs the picker offers, each with the user it routes to: `[(employee, user or None)]`."""
    rows = Employee.objects.active().filter(role=EmployeeRole.HOD).order_by('employee_name')
    for word in (search or '').split():
        rows = rows.filter(Q(employee_name__icontains=word) | Q(employee_code__icontains=word))
    users = _active_users()
    return [(e, match_user(e, users)) for e in rows]


def head_by_code(code):
    """An active HOD by employee code, and their user: `(employee, user)`; either may be None."""
    employee = (Employee.objects.active().filter(role=EmployeeRole.HOD, employee_code=(code or '').strip().upper())
                .first())
    return employee, (match_user(employee) if employee else None)
