"""The helper both `assignments` and `accounts` need.

`_get_user_assignment_category` answers "which product category is this user
scoped to", which the assignment screens use to filter what may be assigned and
the account screens use to decide what to show. It is the only name the split
of `users/views.py` (plan item 3.3) could not give to one side.
"""





def _get_user_assignment_category(user):
    category_obj = getattr(user, 'category', None)
    return _normalize_category(getattr(category_obj, 'category', None))






def _normalize_category(value):
    normalized = str(value or '').strip().upper()
    return normalized or None
