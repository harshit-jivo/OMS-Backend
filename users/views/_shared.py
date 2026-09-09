"""The helper both `assignments` and `accounts` need.

`_get_user_assignment_category` answers "which product category is this user
scoped to", which the assignment screens use to filter what may be assigned and
the account screens use to decide what to show. It is the only name the split
of `users/views.py` (plan item 3.3) could not give to one side.
"""





def _get_user_assignment_category(user):
    category_obj = getattr(user, 'category', None)
    return _normalize_category(getattr(category_obj, 'category', None))






def _get_user_assignment_categories(user):
    """All categories assigned to the user (normalized), falling back to the
    single primary `category` FK for users created before multi-category.

    Lives here rather than in `assignments`, because `orders.views.masters`
    needs it too: the party picker scopes to the categories a user actually
    works in, and a helper that answers "which categories is this user scoped
    to" should have one definition, not one per app that asks.
    """
    names = []
    seen = set()
    manager = getattr(user, 'categories', None)
    if manager is not None:
        try:
            for cat in manager.all():
                name = _normalize_category(getattr(cat, 'category', cat))
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        except Exception:
            names = []
    if names:
        return names
    primary = _get_user_assignment_category(user)
    return [primary] if primary else []


def _normalize_category(value):
    normalized = str(value or '').strip().upper()
    return normalized or None
