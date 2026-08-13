"""Models for the notification framework.

Intentionally EMPTY in Phase 3.1.

`Notification`, `PushToken` and `WebPushSubscription` still live in
`orders/models.py` and are still owned by the orders app. They move here in a
later phase using `SeparateDatabaseAndState`, which changes only Django's idea
of which app owns each model — the physical tables, their indexes and their
foreign keys are never touched, and not one row moves.

Defining anything here now would be actively harmful: two model classes
claiming the same `related_name` on `User` (`notifications`, `push_tokens`,
`web_push_subscriptions`) and on `Order` (`notifications`) is a
`fields.E304`/`E305` clash, and `manage.py check` would fail. There is no valid
half-state — the move must land in a single commit.
"""
