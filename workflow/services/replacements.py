"""Date-based effective-actor resolution — plan §8.

Resolution is a pure function of (user, date). It never touches the user's
account: no `is_active` flip, no role change, no write to `users_user` of any
kind. Only which user the engine treats as the actor changes.

Two properties worth stating, because both are load-bearing:

* **At most one row can match.** The GiST exclusion constraint on
  `workflow_user_replacements` bars overlapping windows for one `old_user`, so
  `.first()` here is deterministic rather than order-dependent. Without that
  constraint this function would silently depend on row order.

* **Resolution is dynamic, not baked in.** `WorkflowStage.user` holds the
  CONFIGURED user; the effective actor is computed on every read. That is what
  makes "after `end_date` the original user automatically resumes" true for
  work that was ALREADY OPEN when the window closed. A module that copies the
  effective user onto its own task row at creation time is choosing the other
  behaviour — it should re-resolve through here when it wants the dynamic one.
"""
from django.utils import timezone

from workflow.models import WorkflowUserReplacement


def today():
    """The engine's idea of 'now', as a date in the project timezone.

    Resolved once per operation and passed down, so a single operation cannot
    straddle midnight and see two different effective actors.
    """
    return timezone.localdate()


def replacement_for(user_id, on_date=None):
    """The active replacement row for `user_id`, or None."""
    on_date = on_date or today()
    return (
        WorkflowUserReplacement.objects
        .filter(old_user_id=user_id,
                is_active=True,
                start_date__lte=on_date,
                end_date__gte=on_date)
        .first()
    )


def effective_user_id(user_id, on_date=None):
    """Who actually acts for `user_id` on `on_date`.

    Single hop, deliberately NOT transitive: if 5 is replaced by 8 and 8 is
    separately replaced by 12, this returns 8 for user 5. Following chains
    invites cycles (5->8, 8->5) and makes accountability opaque; one hop is
    predictable and always terminates.
    """
    replacement = replacement_for(user_id, on_date)
    return replacement.new_user_id if replacement else user_id


def effective_user_ids(user_ids, on_date=None):
    """Batch form of `effective_user_id` — one query for many users.

    Used by the inbox so a list of tasks does not issue one replacement query
    per row (N+1).
    """
    on_date = on_date or today()
    user_ids = list(user_ids)
    if not user_ids:
        return {}
    rows = (
        WorkflowUserReplacement.objects
        .filter(old_user_id__in=user_ids,
                is_active=True,
                start_date__lte=on_date,
                end_date__gte=on_date)
        .values_list('old_user_id', 'new_user_id')
    )
    mapping = {old: new for old, new in rows}
    return {uid: mapping.get(uid, uid) for uid in user_ids}


def configured_users_acting_for(user_id, on_date=None):
    """Configured users whose work `user_id` currently owns.

    The inverse lookup the inbox needs: given the caller, which stage users do
    they currently stand in for? Returns `user_id` itself plus everyone
    replaced by them today — unless `user_id` is themselves replaced, in which
    case their own tasks belong to their stand-in and are excluded.
    """
    on_date = on_date or today()
    acting_for = set(
        WorkflowUserReplacement.objects
        .filter(new_user_id=user_id,
                is_active=True,
                start_date__lte=on_date,
                end_date__gte=on_date)
        .values_list('old_user_id', flat=True)
    )
    # Own tasks count only while someone else is not standing in for them.
    if not replacement_for(user_id, on_date):
        acting_for.add(user_id)
    return acting_for
