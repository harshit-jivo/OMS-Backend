# Domain relationship graph

Generated from the model registry. `->` is a concrete FK or M2M.

## Cross-app coupling

Every edge that crosses an app boundary. These are the seams: an app with
no inbound cross-app edges can be extracted; one with many cannot.

| from app | to app | edges |
|---|---|---:|
| `orders` | `users` | 18 |
| `tracker` | `users` | 9 |
| `approvals` | `users` | 7 |
| `invoice` | `users` | 4 |
| `payments` | `users` | 3 |
| `users` | `orders` | 2 |
| `notifications` | `users` | 2 |
| `orders` | `sap_sync` | 1 |
| `audit` | `users` | 1 |
| `devices` | `users` | 1 |
| `attachments` | `users` | 1 |

### Detail


**`approvals` → `users`**

- `approvals.ApprovalAction.approver -> users.User (FK/PROTECT)`
- `approvals.ApprovalLevel.role -> users.UserRole (FK/PROTECT)`
- `approvals.ApprovalLevelApprover.assigned_by -> users.User (FK/SET_NULL)`
- `approvals.ApprovalLevelApprover.user -> users.User (FK/CASCADE)`
- `approvals.ApprovalRequest.created_by -> users.User (FK/SET_NULL)`
- `approvals.ApprovalRequest.submitted_by -> users.User (FK/PROTECT)`
- `approvals.ApprovalWorkflow.created_by -> users.User (FK/SET_NULL)`

**`attachments` → `users`**

- `attachments.Attachment.uploaded_by -> users.User (FK/PROTECT)`

**`audit` → `users`**

- `audit.AuditLog.user -> users.User (FK/SET_NULL)`

**`devices` → `users`**

- `devices.UserDevice.user -> users.User (FK/CASCADE)`

**`invoice` → `users`**

- `invoice.CreditLimitLogs.created_by -> users.User (FK/CASCADE)`
- `invoice.InvoiceLog.created_by -> users.User (FK/CASCADE)`
- `invoice.InvoiceLog.deleted_by -> users.User (FK/SET_NULL)`
- `invoice.InvoiceRefLogs.posted_by -> users.User (FK/CASCADE)`

**`notifications` → `users`**

- `notifications.Notification.company -> users.Company (FK/CASCADE)`
- `notifications.Notification.user -> users.User (FK/CASCADE)`

**`orders` → `sap_sync`**

- `orders.StaffProductPrice.product -> sap_sync.Product (FK/CASCADE)`

**`orders` → `users`**

- `orders.Notification.user -> users.User (FK/CASCADE)`
- `orders.Order.approved_by -> users.User (FK/SET_NULL)`
- `orders.Order.created_by -> users.User (FK/SET_NULL)`
- `orders.Order.quotation_cancelled_by -> users.User (FK/SET_NULL)`
- `orders.Order.rejected_by -> users.User (FK/SET_NULL)`
- `orders.OrderFlowConfig.updated_by -> users.User (FK/SET_NULL)`
- `orders.OrderItem.scheme -> users.SchemeProduct (FK/SET_NULL)`
- `orders.OrderItemApprovalMapping.approver -> users.User (FK/CASCADE)`
- `orders.OrderItemScheme.scheme -> users.SchemeProduct (FK/SET_NULL)`
- `orders.OrderRateApproval.approver -> users.User (FK/CASCADE)`
- `orders.OrdersLog.performed_by -> users.User (FK/SET_NULL)`
- `orders.PartyOrderFlowConfig.updated_by -> users.User (FK/SET_NULL)`
- `orders.PushToken.user -> users.User (FK/CASCADE)`
- `orders.RateApproverRule.approver -> users.User (FK/CASCADE)`
- `orders.Scheme.created_by -> users.User (FK/SET_NULL)`
- `orders.SchemeAssignment.created_by -> users.User (FK/SET_NULL)`
- `orders.Template.user -> users.User (FK/CASCADE)`
- `orders.WebPushSubscription.user -> users.User (FK/CASCADE)`

**`payments` → `users`**

- `payments.BankDeposit.created_by -> users.User (FK/SET_NULL)`
- `payments.PaymentReceipt.created_by -> users.User (FK/SET_NULL)`
- `payments.PaymentStatusHistory.changed_by -> users.User (FK/SET_NULL)`

**`tracker` → `users`**

- `tracker.AlertNotification.user -> users.User (FK/CASCADE)`
- `tracker.CashVoucher.created_by -> users.User (FK/SET_NULL)`
- `tracker.Invoice.created_by -> users.User (FK/PROTECT)`
- `tracker.Invoice.deleted_by -> users.User (FK/SET_NULL)`
- `tracker.PaymentDetail.updated_by -> users.User (FK/SET_NULL)`
- `tracker.StageEvent.acted_by -> users.User (FK/SET_NULL)`
- `tracker.TransporterPayment.created_by -> users.User (FK/SET_NULL)`
- `tracker.UserStageAccess.assigned_by -> users.User (FK/SET_NULL)`
- `tracker.UserStageAccess.user -> users.User (FK/CASCADE)`

**`users` → `orders`**

- `users.User.categories -> orders.Categories (M2M)`
- `users.User.category -> orders.Categories (FK/PROTECT)`


## Most-referenced models

What the rest of the system leans on. Changing these is expensive.

| model | inbound refs | from |
|---|---:|---|
| `users.User` | 48 | `approvals`, `attachments`, `audit`, `devices`, `invoice`, `notifications`, `orders`, `payments`, `tracker`, `users` |
| `orders.Order` | 6 | `orders` |
| `tracker.Stage` | 5 | `tracker` |
| `orders.Scheme` | 4 | `orders` |
| `tracker.Invoice` | 4 | `tracker` |
| `users.SchemeProduct` | 3 | `orders`, `users` |
| `users.State` | 3 | `users` |
| `users.UserRole` | 3 | `approvals`, `users` |
| `invoice.InvoiceLog` | 3 | `invoice` |
| `payments.PaymentReceipt` | 3 | `payments` |
| `users.Company` | 2 | `notifications`, `users` |
| `users.MainGroup` | 2 | `users` |
| `orders.OrderStatus` | 2 | `orders` |
| `orders.OrderItem` | 2 | `orders` |
| `orders.Categories` | 2 | `users` |
| `einvoice.IrnRecord` | 2 | `einvoice` |
| `tracker.Unit` | 2 | `tracker` |
| `approvals.ApprovalWorkflow` | 2 | `approvals` |
| `payments.CollectionPerson` | 2 | `payments` |
| `HAIS.Department` | 2 | `HAIS` |


## Models nothing references

Aggregate roots, log tables, or dead weight — worth knowing which.

- **`HAIS`**: `AssetLog`, `AssetStorageType`
- **`SKU`**: `SKU`
- **`approvals`**: `ApprovalAction`, `ApprovalLevelApprover`
- **`attachments`**: `Attachment`
- **`audit`**: `AuditLog`
- **`core`**: `DocumentCounter`
- **`devices`**: `UserDevice`, `VersionPolicy`
- **`einvoice`**: `EwayBill`, `IrnGenerationLog`
- **`invoice`**: `CreditLimitLogs`, `InvocieHistory`, `InvoiceRefLogs`
- **`legal`**: `LabelData`, `LabelNutrition`
- **`notifications`**: `Notification`
- **`orders`**: `Branches`, `DispatchLocation`, `Notification`, `OrderFlowConfig`, `OrderItemApprovalMapping`, `OrderItemScheme`, `OrderRateApproval`, `OrdersLog`, `Parties`, `PartyAddress`, `PartyOrderFlowConfig`, `ProductDetails`, `PushToken`, `RateApproverRule`, `SchemeAssignment`, `SchemeTrigger`, `StaffProductPrice`, `Template`, `WebPushSubscription`
- **`payments`**: `BankDepositLine`, `CashDenomination`, `PaymentAllocation`, `PaymentMethodMapping`, `PaymentStatusHistory`, `SapCallLog`, `SapCompanyMap`
- **`sap_sync`**: `Branch`, `Party`, `PartyAddress`, `SalesOrderLog`, `SalesQuotationLog`, `SyncLog`, `SyncSchedule`
- **`tracker`**: `AlertNotification`, `CashVoucher`, `PaymentDetail`, `StageEvent`, `TransporterPayment`, `UserStageAccess`
- **`uilabels`**: `UILabel`
- **`users`**: `PartyProductAssignment`, `UserPartyAssignment`, `UserState`


## Unmanaged tables

Django will not create or migrate these; the schema is owned elsewhere.

- `orders.Branches` → `branches`
- `sap_sync.Branch` → `branches`
- `users.UserState` → `users_user_states`

---

**98 models, 121 relational edges, 49 of them crossing app boundaries.**
