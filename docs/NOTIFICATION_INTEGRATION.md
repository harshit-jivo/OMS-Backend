# Notification Integration Guide

> **READ THIS FILE BEFORE INTEGRATING NOTIFICATIONS INTO ANY NEW MODULE.**
>
> The single acceptance question for this whole system is:
>
> > *"Can I add a new business module tomorrow and integrate notifications by
> > following this document, without changing anything inside `notifications/`?"*
>
> If the answer is ever **no**, stop and fix the integration design — do **not**
> modify the framework. Two modules (Payments receipts, Bank Deposits) have
> already integrated with **zero** changes to `notifications/`. A third should be
> no different.

This guide describes the **actual** implementation in this repository, verified
against the code (Phase 3.1–3.7). It documents what exists — not planned work.

---

## A. Purpose

`notifications/` is a **reusable, business-module-agnostic notification
framework**. A business module tells it:

- **what** happened (an event name),
- **who** should be told (recipients),
- **which** object it concerns (an entity).

The framework then owns everything else:

- persisting a `Notification` record,
- building one canonical payload,
- delivering it over every channel (mobile push via Expo, web push),
- isolating provider failures so a push problem never breaks the business
  transaction.

The framework knows nothing about payments, deposits, orders, or invoices.

---

## B. Architecture

```
Business module            notifications framework            transport
---------------            -----------------------            ---------
event + recipients   -->   Notification (persistence)
+ entity                        |
                                v
                          canonical payload
                                |
                 +--------------+--------------+
                 v                             v
           MobileProvider                 WebProvider
                 |                             |
                 v                             v
             resolver                      resolver
          (user -> tokens)          (user -> subscriptions)
                 |                             |
                 v                             v
                Expo                       Web Push
```

- **Business module** owns the event name, the recipient rule, the wording, and
  the company rule. It calls `notifications.notify(...)`.
- **Framework** persists the `Notification`, builds the payload, and dispatches
  delivery after the transaction commits.
- **Providers** (`MobileProvider`, `WebProvider`) are generic transports. They
  do not know what a payment or a deposit is.
- **Resolvers** map a `user` to that user's existing device tokens / web
  subscriptions. They are configured by **dotted path in settings**, so the
  framework never imports the module that owns token storage.

---

## C. Dependency rule (the one that matters)

**Correct:**

```
business module  ->  notifications
```

**Forbidden:**

```
notifications  ->  business module
```

`notifications/` must never `import orders / payments / deposits / invoices /
approvals / inventory` or any other business module. This is enforced in
practice: `grep -rn "import orders\|import payments\|..." notifications/` returns
nothing.

The **entity** is handled as an opaque Django model instance via a
`GenericForeignKey`; **recipients** are opaque `User` instances. That is the
whole reason the framework can serve any module without knowing about it.

---

## D. Database model — `notifications.Notification`

| Field          | Type                                   | Meaning                                              |
| -------------- | -------------------------------------- | ---------------------------------------------------- |
| `user`         | FK → `AUTH_USER_MODEL` (CASCADE)       | The recipient.                                       |
| `company`      | FK → `users.Company` (null)            | Company scope (see **Company isolation**).           |
| `event_type`   | `CharField(50)`, indexed               | The business event name (e.g. `DEPOSIT_APPROVED`).   |
| `title`        | `CharField`                            | Presentation, supplied by the caller.                |
| `message`      | `TextField`                            | Presentation, supplied by the caller.                |
| `content_type` | FK → `ContentType` (PROTECT, null)     | The entity's model — half of the GenericForeignKey.  |
| `object_id`    | `PositiveBigIntegerField` (null)       | The entity's pk — the other half.                    |
| `entity`       | `GenericForeignKey('content_type', 'object_id')` | The business object, generically.        |
| `is_read`      | `BooleanField`                         | Read state.                                          |
| `created_at`   | `DateTimeField`                        | Creation time.                                       |

`entity` is **generic**. It can point at an `Order`, a `PaymentReceipt`, a
`BankDeposit`, an `Invoice`, or any future Django model — with **no schema
change** to `Notification`.

**Never** add `order_id`, `payment_id`, `deposit_id`, or any module-specific FK
to this model. If you feel the need to, you are breaking the abstraction.

---

## E. Table

The framework persists to:

```
public.notifications_notification
```

This is **deliberately separate** from the old Orders notification table:

```
public.notifications          (old Orders system — leave it alone)
```

The old Orders notification system (`public.notifications`,
`public.push_tokens`, `public.web_push_subscriptions`) is **operational and
untouched**. New-module integration must **never**:

- delete / migrate / rename old notification rows or tables,
- change the old `PushToken` / `WebPushSubscription` schema,
- remove Orders notification code,
- create duplicate token tables.

Integrating a new module requires **no migration** — you are adding no model.

---

## F. Event ownership

**Business modules own their event names.** The framework only validates the
*format* of a name (`notifications.constants.is_valid_event_name`): uppercase,
underscore-separated, starting with a letter — e.g. `^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$`.

Real examples in this repo:

| Module            | Events                                                         | Where they live                   |
| ----------------- | ------------------------------------------------------------- | --------------------------------- |
| Payments receipts | `PAYMENT_SUBMITTED`, `PAYMENT_APPROVED`, `PAYMENT_REJECTED`    | `payments/notification_events.py` |
| Bank Deposits     | `DEPOSIT_SUBMITTED`, `DEPOSIT_APPROVED`, `DEPOSIT_REJECTED`    | `payments/notification_events.py` |
| (future) Invoices | `INVOICE_SUBMITTED`, `INVOICE_APPROVED`, `INVOICE_REJECTED`    | `invoices/notification_events.py` |

> **Do NOT add every business event name to `notifications/constants.py`.** The
> constants module ships a few anticipated names, but membership there is **not**
> required — `notify()` validates the name **format only**, not membership. The
> live `*_SUBMITTED` / `*_APPROVED` / `*_REJECTED` events are **not** in
> `constants.EVENT_NAMES`, and work regardless.

### The `SUBMITTED` / `APPROVED` / `REJECTED` lifecycle — and who each notifies

A document that flows through an approval workflow has a three-event lifecycle,
and **each event has a different audience**:

| Event            | Fires when…                                             | Recipients                    |
| ---------------- | ------------------------------------------------------- | ----------------------------- |
| `MODULE_SUBMITTED` | the document **enters the approval workflow**          | the **eligible approver(s)**  |
| `MODULE_APPROVED`  | final approval lands                                   | the **submitter**             |
| `MODULE_REJECTED`  | any approver rejects                                   | the **submitter**             |

> **"Created" is NOT "Submitted for approval."** A draft sitting in the database
> notifies no one. The `SUBMITTED` event fires **only** once the document has
> successfully entered the approval workflow and a decision is actually required.
> Do **not** define a `MODULE_CREATED` event that fires on row insert.

Add this lifecycle to a new module **only when the module genuinely has an
approval workflow**. A module with no approval step has no `SUBMITTED` audience.

Concrete audiences in this repo:

```
PAYMENT_SUBMITTED  -> the CURRENT approval level's approvers
PAYMENT_APPROVED   -> Payment submitter (creator)
PAYMENT_REJECTED   -> Payment submitter (creator)

DEPOSIT_SUBMITTED  -> the CURRENT approval level's approvers
DEPOSIT_APPROVED   -> Deposit submitter (creator)
DEPOSIT_REJECTED   -> Deposit submitter (creator)
```

### Multi-level sequencing — the Orders-parity rule

This is the single most important behavioral rule, and it mirrors how the
**Orders** notification system already works (Orders notifies only the stage that
currently owns the document — rate approver, *then* billing, *then* auditor —
never all at once):

```
SUBMIT           -> notify ONLY the current level (level 1) approvers
level 1 clears   -> notify ONLY level 2 approvers      (level 3 gets nothing yet)
level 2 clears   -> notify ONLY level 3 approvers
final level      -> notify the CREATOR (approved)
any level rejects-> notify the CREATOR (rejected); NO downstream level is notified
```

> **Never notify all workflow approvers at submission.** Only the level that is
> *currently active* is notified. The next level is notified **when the workflow
> actually transitions to it**, not before.

**How the transitions are observed (no second approval engine):** the reusable
`approvals` engine owns level state and fires domain hooks:

| Engine event      | When                                    | Payments hook does…                         |
| ----------------- | --------------------------------------- | ------------------------------------------- |
| `on_submitted`    | document enters the workflow (level 1)  | notify current-level approvers              |
| `on_level_advanced` | an intermediate level clears → next rung | notify the NEW current-level approvers    |
| `on_approved`     | the **final** level clears              | notify the creator (`*_APPROVED`)           |
| `on_rejected`     | any level rejects                       | notify the creator (`*_REJECTED`)           |

`on_level_advanced` is a generic engine hook (`approvals.services.register_hooks`),
not a Payments concept — any module with a multi-level workflow reuses it.

**Current-level recipient resolution** is `approvals.services.current_level_approvers(request)`
— it resolves `eligible_approver_ids` for `request.current_level` **only**, so
the notification audience is always exactly the stage that owns the document.
Payments then removes the submitter and de-duplicates
(`payments.notification_events._current_level_recipients`).

`min_approvals` on a level governs when it actually clears (quorum). The next
level is notified only once the engine has genuinely advanced — the notification
observes the workflow, it never decides approval rules.

---

## G. Recipient rule

**The business module decides who receives the notification.** The framework is
handed a ready list:

```python
recipients=[submitter]
```

The framework contains **no** business recipient rule. In this repo:

- **`*_APPROVED` / `*_REJECTED`** notify the **submitter/creator** — resolved
  from `approval_request.submitted_by`.
- **`*_SUBMITTED`** (and each next-level ping) notifies the **CURRENT level's
  approver(s)** — resolved by `approvals.services.current_level_approvers(request)`,
  which returns `eligible_approver_ids` for `request.current_level` **only**
  (never all levels). Payments then **drops the submitter** and de-duplicates
  (`payments.notification_events._current_level_recipients`).

**Reuse the existing approver resolver; do not build a second one.** Approver
selection is owned by `approvals` (a single source of truth used by the inbox,
`can_act`, and the level notifications). The notification framework never learns
how approvers are chosen — it only receives `recipients=[approver1, approver2, ...]`.

Multiple approvers **at the same level** → **one notification per recipient**
(the `Notification` model is user-scoped). Duplicate users (the same person named
on the rung twice) collapse to a single notification. A level-2 approver is
**not** notified while the document sits at level 1.

A different module might notify a creator, a requester, or an approver; that
choice belongs to the module, not the framework.

---

## H. Entity rule

**Always pass the model instance:**

```python
entity=deposit      # or entity=receipt, entity=invoice, entity=order, ...
```

The framework resolves `(content_type, object_id)` from it internally via
`ContentType.objects.get_for_model(...)`.

**Never** manually construct `content_type` / `object_id` at the call site.
**Never** add `payment_id` / `deposit_id` / `order_id` fields to `Notification`.

---

## I. Company isolation

Company scope is decided **server-side**, never from a client-supplied id.

`notify()` behaviour:

- If you pass `company=<users.Company>`, **only** recipients whose
  `company_id` matches are notified; mismatched recipients are **skipped and
  logged**.
- If you **omit** `company`, each recipient's **own** `recipient.company_id` is
  used.

**Repository detail:** `PaymentReceipt.company` and `BankDeposit.company` are SAP
**category strings** (`CATEGORY_CHOICES`), *not* `users.Company` foreign keys.
They therefore **cannot** be passed as the framework's `company`. Both Payments
and Deposits omit `company`, so each recipient is scoped to their own
`users.Company` — a document raised by a company-A user notifies that user in
company A, and a company-B user is never notified. This is the "safe server-side
mapping" pattern: if your module's `company` field is not a real `users.Company`
FK, omit `company` and let the framework derive it from the recipient.

If your module **does** have a genuine `users.Company` FK, you may pass it
explicitly to enforce cross-recipient isolation.

Never trust `request.user`-supplied or payload `company_id` for notification
scope.

---

## J. `notify()` API

The actual current signature
(`notifications/services/dispatcher.py`, re-exported from
`notifications.services`):

```python
notify(
    *,
    event_type,     # str — validated by format (is_valid_event_name). Module-owned.
    title,          # str — presentation. Framework never infers type from it.
    message,        # str — presentation.
    recipients,     # iterable of User instances. Each is type-checked.
    entity=None,    # a model instance; resolved to (content_type, object_id).
    company=None,   # users.Company; if given, only matching recipients are notified.
    actor=None,     # the user who triggered it (logged, not persisted).
)
```

Returns the list of created `Notification` records (possibly empty).

Behaviour to rely on:

- Invalid `event_type` format → `ValueError` (fail fast, at the call site).
- A non-`User` recipient → `TypeError`.
- Records are created **synchronously** in the caller's transaction.
- Delivery is scheduled with `transaction.on_commit` (see **Transaction
  behavior**).

---

## K. Event implementation — the standard module pattern

Each module gets one small events module: `<module>/notification_events.py`. It
owns the event names, the recipient-agnostic publish helper, and the wording. It
delegates all persistence/delivery to `notify()`.

**Real example — Bank Deposits (`payments/notification_events.py`):**

```python
DEPOSIT_APPROVED = "DEPOSIT_APPROVED"
DEPOSIT_REJECTED = "DEPOSIT_REJECTED"


def publish_deposit_decision(deposit, submitter, *, approved, actor=None, reason=""):
    if submitter is None:
        return []

    from notifications.services import notify

    if approved:
        event_type = DEPOSIT_APPROVED
        title = "Deposit Approved"
        message = f"Deposit {deposit.deposit_no} has been approved."
    else:
        event_type = DEPOSIT_REJECTED
        title = "Deposit Rejected"
        message = f"Deposit {deposit.deposit_no} has been rejected."
        if reason:
            message += f" Reason: {reason}"

    return notify(
        event_type=event_type,
        title=title,
        message=message,
        recipients=[submitter],
        entity=deposit,          # generic entity — no deposit_id anywhere
        actor=actor,
    )
```

**Where it is called — the approval hook
(`payments/hooks.py`, `_on_deposit_approved`):**

```python
approver = getattr(approval_request, '_acting_user', None)
transaction.on_commit(lambda: post_deposit_to_sap(deposit, user=approver))

from .notification_events import publish_deposit_decision
publish_deposit_decision(
    deposit, approval_request.submitted_by, approved=True, actor=approver,
)
```

Payments receipts follow the identical shape (`publish_receipt_decision`,
`_on_receipt_approved` / `_on_receipt_rejected`). Copy the *shape*, not the
wording — each module writes its own titles/messages.

**The `SUBMITTED` variant — notifying approvers at the submission boundary.**
The publisher resolves approvers (not the submitter) and fires from **inside the
submission service's `@transaction.atomic` block**, right after the document
has successfully entered the workflow:

```python
# payments/notification_events.py
def publish_receipt_submitted(receipt, submitter):
    recipients = _approval_recipients(receipt, submitter)   # approvers − submitter, deduped
    if not recipients:
        return []
    from notifications.services import notify
    return notify(
        event_type=PAYMENT_SUBMITTED,
        title="Payment Approval Required",
        message=f"Payment {receipt.receipt_no} has been submitted and requires your approval.",
        recipients=recipients,
        entity=receipt,
        actor=submitter,
    )
```

```python
# payments/services.py — submit_receipt(), inside @transaction.atomic,
# AFTER approval_services.submit(...) has opened the approval request:
from .notification_events import publish_receipt_submitted
publish_receipt_submitted(receipt, user)
```

`_approval_recipients` finds the document's open (`PENDING`) `ApprovalRequest`
by `(content_type, object_id)`, calls
`approvals.services.eligible_approvers_for_request`, removes the submitter, and
de-duplicates. The **submission boundary** is the exact line where
`approvals.services.submit(...)` succeeds — not where the draft row was first
created. Deposits follow the identical shape (`publish_deposit_submitted`,
wired in `submit_deposit`).

---

## L. Transaction behavior

- Notification **records** are created in the **current transaction**. If the
  caller's `atomic()` block rolls back, the records roll back with it.
- External **delivery** is scheduled with `transaction.on_commit`, so it runs
  **only after a successful commit** (or immediately, in autocommit).

Therefore:

- rollback → **no** notification record,
- rollback → **no** push delivery.

The deposit and receipt hooks run **inside** the approval transaction, so the
notification commits together with the approval — both or neither. Do not
overstate this: it is on-commit delivery, not an independent autocommit write.

---

## M. Mobile delivery

- `MobileProvider` (`notifications/providers/mobile.py`) POSTs the canonical
  payload to Expo (`https://exp.host/--/api/v2/push/send`).
- It resolves the recipient's device tokens through a **configured resolver**,
  never by importing a business module.

Setting actually used by this project:

```
NOTIFICATION_MOBILE_TOKEN_RESOLVER
    default: "orders.notification_resolvers.resolve_mobile_tokens"
```

`orders/notification_resolvers.py` **reads** (never writes) the existing
`push_tokens` table and returns a list of active token strings for that user. No
new token table is created for any module.

### Mobile deep-link (tap → detail screen)

When a push is tapped, the app routes from the **generic** payload, never from a
module-specific id. `OMS-app-real/src/utils/notificationRouting.ts`:

- Order notifications carry `order_id` and open the Order detail screen
  (unchanged — they short-circuit before the generic branch).
- Framework notifications carry `entity_type` + `entity_id`. The router maps
  `entity_type` → screen via an `ENTITY_ROUTES` table:

  ```
  "paymentreceipt" -> /(main)/payments/tracking-details  { id, kind: "PAYMENT" }
  "bankdeposit"    -> /(main)/payments/tracking-details  { id, kind: "DEPOSIT" }
  ```

  `tracking-details` is the unified detail screen that loads a receipt or a
  deposit purely from `{ id, kind }`. **To support a future module, add one row
  to `ENTITY_ROUTES`** — no other router change. Unknown `entity_type`s fall back
  to the notification inbox (never a dead tap).

The exact `entity_type` strings are the backend's `content_type.model` values —
verify them against the canonical payload, don't guess.

---

## N. Web delivery

- `WebProvider` (`notifications/providers/web.py`) sends an encrypted Web Push
  via `pywebpush`, signed with the project VAPID keys (read from settings).
- It resolves the recipient's subscriptions through a **configured resolver**.

Setting actually used by this project:

```
NOTIFICATION_WEB_SUBSCRIPTION_RESOLVER
    (also accepts the alias NOTIFICATION_SUBSCRIPTION_RESOLVER)
    default: "orders.notification_resolvers.resolve_web_subscriptions"
```

`orders/notification_resolvers.py` reads the existing
`web_push_subscriptions` table and returns
`{"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}` dicts for that user.

If a resolver setting is blank/unset, that channel is a **safe no-op** — nothing
is sent, nothing errors.

---

## O. Provider error isolation

Delivery is fully isolated (`dispatcher._deliver`):

- one failed push must not break **another recipient**,
- one failed **provider** must not break **another provider**,
- no delivery failure may bubble up into the **business transaction** (a push
  problem must never turn an approval into an HTTP 500).

Failures are caught, logged, and swallowed. Notification records are still
created; delivery is best-effort.

---

## P. Canonical payload

There is **one** payload builder (`notifications/services/payloads.py`), shared
by every channel:

```python
{
    "notification_id": <int>,
    "event_type":      <str>,        # e.g. "DEPOSIT_APPROVED"
    "title":           <str>,
    "message":         <str>,
    "entity_type":     <str|null>,   # content_type.model, e.g. "bankdeposit"
    "entity_id":       <int|null>,   # object_id
    "company_id":      <int|null>,
    "screen":          "notification",
}
```

Clients route by the **generic** `(entity_type, entity_id)` — never by a
module-specific id. There is no `deposit_id` / `payment_id` / `order_id` in the
payload.

---

## Q. How to add a new module — checklist

1. **Read this document.**
2. Audit the module's lifecycle — find the **real, definitive** transition
   points, not speculative ones. If the module has an approval workflow, it has
   up to three: **submission** (→ approvers), **approval** and **rejection**
   (→ submitter). "Row created" is **not** a notification event.
3. Find the **submission boundary** — the exact line where the module calls
   `approvals.services.submit(...)` (or otherwise commits "now awaiting
   approval"). The `SUBMITTED` event fires immediately after that succeeds,
   never on draft insert.
4. Identify the events (e.g. `INVOICE_SUBMITTED`, `INVOICE_APPROVED`,
   `INVOICE_REJECTED`).
5. Identify recipients from the actual business rule:
   - decision events → the submitter (`approval_request.submitted_by`);
   - submission event → the eligible approvers via
     `approvals.services.eligible_approvers_for_request(request)`, minus the
     submitter, de-duplicated. **Reuse that resolver — do not write a new one.**
6. Identify the company source (real `users.Company` FK → pass it; category
   string / other → omit `company`, let the framework use the recipient's).
7. Identify the transaction boundary where each outcome is committed (submission
   fires inside the submission service's `atomic` block; approve/reject fire
   inside the approval hook).
8. Create `<module>/notification_events.py` with the event names + the
   `publish_<x>_submitted(...)` / `publish_<x>_decision(...)` helpers.
9. Call `notifications.notify(...)` from those helpers.
10. Pass `entity=<model_instance>` — never build `content_type`/`object_id`.
11. Wire the helpers into the module's submission service + approval hooks.
12. Add module notification tests (see `payments/tests_notifications_submitted.py`
    and the deposit suite as templates).
13. Test the mobile payload (`entity_type`/`entity_id`/`event_type`).
14. Test the web payload.
15. Test rollback → no record, no delivery.
16. Test company isolation; for `SUBMITTED`, test submitter-exclusion,
    multiple-approver fan-out, and duplicate-approver de-dupe.
17. Run the framework regression suite.
18. **Verify `notifications/` was not modified.** If it had to be, the
    abstraction failed — fix the integration, not the framework.
19. Update this document **only** if the generic contract itself legitimately
    changed.

---

## R. Example — a hypothetical Invoice module (illustration only)

> This is an **example**, not an implementation. No Invoice integration exists
> yet.

`invoices/notification_events.py` — the full three-event lifecycle:

```python
INVOICE_SUBMITTED = "INVOICE_SUBMITTED"
INVOICE_APPROVED = "INVOICE_APPROVED"
INVOICE_REJECTED = "INVOICE_REJECTED"


def publish_invoice_submitted(invoice, submitter):
    """Notify the eligible approvers that an invoice needs approval."""
    from approvals.models import ApprovalRequest
    from approvals.services import eligible_approvers_for_request

    request = (ApprovalRequest.objects
               .filter(status=ApprovalRequest.Status.PENDING,
                       content_type__app_label=invoice._meta.app_label,
                       content_type__model=invoice._meta.model_name,
                       object_id=invoice.pk)
               .select_related("workflow").order_by("-created_at").first())
    if request is None:
        return []
    recipients = [u for u in eligible_approvers_for_request(request)
                  if u.pk != getattr(submitter, "pk", None)]
    if not recipients:
        return []

    from notifications.services import notify
    return notify(
        event_type=INVOICE_SUBMITTED,
        title="Invoice Approval Required",
        message=f"Invoice {invoice.invoice_no} has been submitted and requires your approval.",
        recipients=recipients,
        entity=invoice,
        actor=submitter,
    )


def publish_invoice_decision(invoice, submitter, *, approved, actor=None, reason=""):
    if submitter is None:
        return []

    from notifications.services import notify

    if approved:
        event_type, title = INVOICE_APPROVED, "Invoice Approved"
        message = f"Invoice {invoice.invoice_no} has been approved."
    else:
        event_type, title = INVOICE_REJECTED, "Invoice Rejected"
        message = f"Invoice {invoice.invoice_no} has been rejected."
        if reason:
            message += f" Reason: {reason}"

    return notify(
        event_type=event_type,
        title=title,
        message=message,
        recipients=[submitter],
        entity=invoice,
        actor=actor,
    )
```

Then wire `publish_invoice_submitted(invoice, user)` inside the invoice
**submission** service (after `approvals.services.submit(...)` succeeds, inside
its `atomic` block), and `publish_invoice_decision(...)` inside the invoice
**approval hooks**. That is the **entire** backend surface. No change to
`notifications/`. The payload's `entity_type` becomes `"invoice"` automatically;
clients route on it.

---

## S. What NOT to do

- ❌ Modifying `notifications/` for every new module.
- ❌ Adding `payment_id` / `deposit_id` / `order_id` (or any module FK) to
  `Notification`.
- ❌ Creating module-specific notification tables.
- ❌ Creating duplicate push-token / web-subscription tables.
- ❌ Importing a business module from `notifications/`.
- ❌ Putting recipient business rules inside `notifications/`.
- ❌ Putting company business rules inside `notifications/`.
- ❌ Sending raw device tokens from the client.
- ❌ Manually constructing `ContentType` / `object_id` at the call site.
- ❌ Bypassing `notify()` and writing `Notification` rows directly.
- ❌ Sending real push notifications from tests (always mock Expo / Web Push).
- ❌ Changing the old Orders notification tables during new-module integration.

---

## T. Acceptance checklist

A module's notification integration is complete only when:

- [ ] events are owned by the module (in `<module>/notification_events.py`)
- [ ] recipients are owned by the module
- [ ] entity is passed generically (`entity=<instance>`)
- [ ] company isolation is verified
- [ ] transaction behavior is verified (rollback → nothing)
- [ ] mobile payload is verified
- [ ] web payload is verified
- [ ] provider failure is isolated
- [ ] `notifications/` is unchanged
- [ ] no new token storage was created
- [ ] no schema migration was required
- [ ] the regression suite passes

---

## Reference — files in the current implementation

| Concern                         | File                                             |
| ------------------------------- | ------------------------------------------------ |
| Public API (`notify`)           | `notifications/services/dispatcher.py`           |
| Canonical payload               | `notifications/services/payloads.py`             |
| Model                           | `notifications/models.py`                        |
| Event-name format validator     | `notifications/constants.py`                     |
| Mobile provider                 | `notifications/providers/mobile.py`              |
| Web provider                    | `notifications/providers/web.py`                 |
| Resolver seam (settings-driven) | `notifications/providers/resolvers.py`           |
| Concrete token/sub resolvers    | `orders/notification_resolvers.py` (reads only)  |
| Payments receipt integration    | `payments/notification_events.py`, `payments/hooks.py` |
| Bank Deposit integration        | `payments/notification_events.py`, `payments/hooks.py` |
| Resolver settings               | `OMS/settings.py` (`NOTIFICATION_*_RESOLVER`)    |
