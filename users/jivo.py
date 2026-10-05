"""OMS's side of Jivo Auth: finding Jivo users, and giving one an OMS row.

People are created in Jivo Auth (admin → Users → Add) and granted the `oms`
application there. OMS never creates an account or sets a password; its own
"create user" screens pick an existing Jivo user and give them an OMS row
with roles and assignments. See docs/jivo-auth-integration.md.

Every lookup goes through the application's API key
(`settings.JIVO_AUTH['API_KEY']`), and only users granted OMS come back.
"""
import logging

from django.db import transaction
from rest_framework.exceptions import APIException, ValidationError

from jivo_auth.client import AuthClient
from jivo_auth.exceptions import AuthServiceError
from jivo_auth.users import get_local_user

from users.models import User

logger = logging.getLogger(__name__)


class JivoUnavailable(APIException):
    status_code = 503
    default_detail = "Jivo Auth can't be reached. Try again shortly."
    default_code = 'jivo_unavailable'


def full_name(jivo):
    """Jivo's first and last name as OMS's single `name`, or '' when blank."""
    return ' '.join(
        part.strip()
        for part in (jivo.get('first_name'), jivo.get('last_name'))
        if part and part.strip()
    )


def _get_users(ids=None):
    try:
        return AuthClient().get_users(ids)
    except AuthServiceError as exc:
        logger.warning('Jivo Auth user lookup failed: %s', exc)
        raise JivoUnavailable() from exc


def jivo_directory():
    """Every Jivo user with OMS access, and the OMS row each one has, if any.

    `oms_user_id` is None for someone who has never signed in and hasn't been
    added yet: those are the people a create form offers.
    """
    jivo_users = _get_users()
    linked = dict(
        User.objects.filter(auth_id__in=[u['id'] for u in jivo_users])
        .values_list('auth_id', 'pk')
    )
    linked = {str(auth_id): pk for auth_id, pk in linked.items()}
    rows = [{
        'auth_id': u['id'],
        'email': u['email'],
        'name': full_name(u),
        'is_active': u.get('is_active', True),
        'oms_user_id': linked.get(str(u['id'])),
    } for u in jivo_users]
    return sorted(rows, key=lambda row: (row['name'] or row['email']).lower())


def jivo_user(auth_id):
    """The Jivo user with this ID, if they have OMS access. ValidationError otherwise."""
    matches = _get_users([auth_id])
    if not matches:
        raise ValidationError(
            {'auth_id': ['No Jivo user with access to OMS has this ID.']})
    return matches[0]


def assert_not_in_oms(jivo):
    """Refuse a Jivo user who already has an OMS row, or whose email does.

    An unlinked row with the same email is someone who existed before Jivo
    Auth (often an inactive user the import skipped): editing that row keeps
    their history, and they are linked to it at their first sign-in. Creating
    a second row would split them in two.
    """
    existing = User.objects.filter(auth_id=jivo['id']).first()
    if existing:
        raise ValidationError({'auth_id': [
            f'{jivo["email"]} already has an OMS user (id {existing.pk}).']})
    same_email = User.objects.filter(
        auth_id__isnull=True, email__iexact=jivo['email']).first()
    if same_email:
        raise ValidationError({'auth_id': [
            f'OMS user id {same_email.pk} already has the email {jivo["email"]}. '
            'Edit that user instead: they are linked to this Jivo account at '
            'their first sign-in.']})


@transaction.atomic
def add_jivo_user(jivo, **fields):
    """Create the OMS row for a Jivo user, with `fields` set on it.

    The username, email and unusable password come from the client package
    (`get_local_user`), exactly as for a user whose first sign-in creates
    their row; `name` is copied from Jivo. Call `assert_not_in_oms` first.
    """
    user = get_local_user({'sub': jivo['id'], 'email': jivo['email']})
    user.name = full_name(jivo) or jivo['email']
    for field, value in fields.items():
        setattr(user, field, value)
    user.save()
    return user
