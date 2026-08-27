# Endpoint inventory — 294 routes

Generated from the live URLconf.

`(none declared)` inherits the DRF default, which is currently **AllowAny**.


## `HAIS` — 18 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/hais/^asset-types/$` | OPTIONS | `AssetTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^asset-types/(?P<pk>[^/.]+)/$` | OPTIONS | `AssetTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^asset-types/(?P<pk>[^/.]+)\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `AssetTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^asset-types\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `AssetTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^assets/$` | OPTIONS | `AssetViewSet` | IsAuthenticated | The asset register. Keyed by Asset ID; carries the full history log. |
| `/api/hais/^assets/(?P<asset_id>[^/]+)/$` | OPTIONS | `AssetViewSet` | IsAuthenticated | The asset register. Keyed by Asset ID; carries the full history log. |
| `/api/hais/^assets/(?P<asset_id>[^/]+)\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `AssetViewSet` | IsAuthenticated | The asset register. Keyed by Asset ID; carries the full history log. |
| `/api/hais/^assets/by-serial/$` | OPTIONS | `AssetViewSet` | IsAuthenticated | The asset register. Keyed by Asset ID; carries the full history log. |
| `/api/hais/^assets/by-serial\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `AssetViewSet` | IsAuthenticated | The asset register. Keyed by Asset ID; carries the full history log. |
| `/api/hais/^assets\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `AssetViewSet` | IsAuthenticated | The asset register. Keyed by Asset ID; carries the full history log. |
| `/api/hais/^departments/$` | OPTIONS | `DepartmentViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^departments/(?P<pk>[^/.]+)/$` | OPTIONS | `DepartmentViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^departments/(?P<pk>[^/.]+)\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `DepartmentViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^departments\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `DepartmentViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^storage-types/$` | OPTIONS | `StorageTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^storage-types/(?P<pk>[^/.]+)/$` | OPTIONS | `StorageTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^storage-types/(?P<pk>[^/.]+)\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `StorageTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |
| `/api/hais/^storage-types\.(?P<format>[a-z0-9]+)/?$` | OPTIONS | `StorageTypeViewSet` | IsAuthenticated | Shared behaviour for the dropdown master endpoints. |

## `SKU` — 4 routes, **4 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/sku/<str:item_code>/` | DELETE,GET,OPTIONS,PATCH,PUT | `SKUDetailView` | AllowAny 🔴 | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/sku/all/` | GET,OPTIONS | `SKUListView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/sku/pending/` | GET,OPTIONS | `SKUPendingList` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/sku/upload/` | OPTIONS,POST | `SKUCreateView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |

## `approvals` — 11 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/approvals/approvers/<int:pk>/` | DELETE,GET,OPTIONS,PATCH,PUT | `LevelApproverDetailView` | IsAuthenticated,IsApprovalAdmin | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/approvals/inbox/` | GET,OPTIONS | `InboxView` | IsAuthenticated | Requests awaiting THIS user's decision. |
| `/api/approvals/levels/` | GET,OPTIONS,POST | `LevelListCreateView` | IsAuthenticated,IsApprovalAdmin | Concrete view for listing a queryset or creating a model instance. |
| `/api/approvals/levels/<int:level_id>/approvers/` | GET,OPTIONS,POST | `LevelApproverListCreateView` | IsAuthenticated,IsApprovalAdmin | Grant / list named approvers on a level. |
| `/api/approvals/levels/<int:pk>/` | DELETE,GET,OPTIONS,PATCH,PUT | `LevelDetailView` | IsAuthenticated,IsApprovalAdmin | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/approvals/requests/` | GET,OPTIONS | `ApprovalRequestListView` | IsAuthenticated | Requests visible to the caller, filterable by status/company/type. |
| `/api/approvals/requests/<int:pk>/` | GET,OPTIONS | `ApprovalRequestDetailView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/approvals/requests/<int:pk>/act/` | OPTIONS,POST | `ApprovalActView` | IsAuthenticated | Approve / reject / cancel one request. |
| `/api/approvals/workflows/` | GET,OPTIONS,POST | `WorkflowListCreateView` | IsAuthenticated,IsApprovalAdmin | Concrete view for listing a queryset or creating a model instance. |
| `/api/approvals/workflows/<int:pk>/` | DELETE,GET,OPTIONS,PATCH,PUT | `WorkflowDetailView` | IsAuthenticated,IsApprovalAdmin | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/approvals/workflows/<int:pk>/preview/` | GET,OPTIONS | `WorkflowPreviewView` | IsAuthenticated,IsApprovalAdmin | Render the ladder a document would take, with resolved approver names. |

## `attachments` — 1 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/payments/attachments/<int:pk>/download/` | GET,OPTIONS | `AttachmentDownloadView` | IsAuthenticated | Stream a stored file after checking permission. |

## `devices` — 7 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/admin/devices/` | GET,OPTIONS | `AdminDeviceListView` | IsAuthenticated,IsAdminRole | GET /api/admin/devices/ — the searchable device table. |
| `/api/admin/devices/<int:pk>/` | GET,OPTIONS | `AdminDeviceDetailView` | IsAuthenticated,IsAdminRole | GET /api/admin/devices/<pk>/ — one device and its user. |
| `/api/admin/devices/analytics/` | GET,OPTIONS | `AdminDeviceAnalyticsView` | IsAuthenticated,IsAdminRole | GET /api/admin/devices/analytics/ — summary cards + chart series. |
| `/api/admin/version-policy/` | GET,OPTIONS,PUT | `AdminVersionPolicyView` | IsAuthenticated,IsAdminRole | GET/PUT /api/admin/version-policy/ — the mobile version policies. |
| `/api/devices/me/` | GET,OPTIONS | `CurrentDevicesView` | IsAuthenticated | GET /api/devices/me/ — the caller's registered devices. |
| `/api/devices/register/` | OPTIONS,POST | `DeviceRegisterView` | IsAuthenticated | POST /api/devices/register/ — idempotent upsert of the caller's device. |
| `/api/devices/update/` | OPTIONS,PUT | `DeviceUpdateView` | IsAuthenticated | PUT /api/devices/update/ — refresh telemetry for an existing device. |

## `einvoice` — 21 routes, **21 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/einvoice/companies/` | GET,OPTIONS | `list_companies` | AllowAny 🔴 | Companies (SAP company DBs) an IRN can be generated against. |
| `/api/einvoice/ewb/` | OPTIONS,POST | `generate_ewb_by_irn` | AllowAny 🔴 | Body: e-Way Bill payload incl. Irn + transport details |
| `/api/einvoice/ewb/<str:irn>/` | GET,OPTIONS | `get_ewb_by_irn` | AllowAny 🔴 | GET the e-Way Bill linked to an IRN. |
| `/api/einvoice/gstin/<str:gstin>/` | GET,OPTIONS | `get_gstin_details` | AllowAny 🔴 | GET GSTIN master details. |
| `/api/einvoice/gstin/<str:gstin>/sync/` | GET,OPTIONS | `sync_gstin` | AllowAny 🔴 | Force a fresh sync of a GSTIN from the GST common portal. |
| `/api/einvoice/health/` | GET,OPTIONS | `health` | AllowAny 🔴 | Quick check that config is present (does NOT hit the NIC API). |
| `/api/einvoice/heartbeat/` | GET,OPTIONS | `heartbeat` | AllowAny 🔴 | Unencrypted NIC heartbeat/ping (no auth). |
| `/api/einvoice/invoices/` | GET,OPTIONS | `list_invoices` | AllowAny 🔴 | List recent SAP invoices that DO NOT yet have an IRN, so the user can generate |
| `/api/einvoice/irn/` | OPTIONS,POST | `generate_irn` | AllowAny 🔴 | POST a full e-invoice JSON payload as the request body, e.g.: |
| `/api/einvoice/irn/<str:irn>/` | GET,OPTIONS | `get_irn_details` | AllowAny 🔴 | GET the details of a previously generated IRN. |
| `/api/einvoice/irn/<str:irn>/qr.png` | ? | `irn_qr_png` | (none declared) 🔴 | GET the NIC signed QR of a stored IRN as a PNG image (for print / <img src>). |
| `/api/einvoice/irn/by-doc/` | GET,OPTIONS | `get_irn_by_doc` | AllowAny 🔴 | Query params: ?doctype=INV&docnum=...&docdate=dd/mm/yyyy |
| `/api/einvoice/irn/cancel/` | OPTIONS,POST | `cancel_irn` | AllowAny 🔴 | Body: { "irn": "...", "reason_code": "2", "remarks": "..." } |
| `/api/einvoice/irn/from-invoice/<int:docentry>/` | GET,OPTIONS,POST | `irn_from_invoice` | AllowAny 🔴 | Build an IRN payload straight from a SAP B1 invoice (OINV DocEntry). |
| `/api/einvoice/irn/rejected/` | GET,OPTIONS | `get_rejected_irns` | AllowAny 🔴 | Query param: ?date=dd/mm/yyyy |
| `/api/einvoice/irn/sample/` | OPTIONS,POST | `generate_irn_sample` | AllowAny 🔴 | Convenience endpoint: builds a minimal valid invoice from the configured |
| `/api/einvoice/irn/validate/` | OPTIONS,POST | `validate_irn` | AllowAny 🔴 | Validate an invoice payload against the GSTN regexes + arithmetic rules |
| `/api/einvoice/logs/` | GET,OPTIONS | `generation_logs` | AllowAny 🔴 | List IRN auto-generation attempts (einvoice_irn_generation_log). |
| `/api/einvoice/logs/retry/` | OPTIONS,POST | `retry_generation` | AllowAny 🔴 | Re-run auto IRN generation for a DocEntry. Body: { "docentry": n, "company_db": "..." } |
| `/api/einvoice/qr/` | OPTIONS,POST | `render_qr` | AllowAny 🔴 | Render any SignedQRCode string into a QR image without a DB lookup. |
| `/api/einvoice/token/` | GET,OPTIONS,POST | `get_token` | AllowAny 🔴 | Run the auth handshake against NIC and report the result. Useful for testing |

## `ewaybill` — 12 routes, **12 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/ewaybill/<str:ewb_no>/` | GET,OPTIONS | `get_ewb` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/ewaybill/cancel/` | OPTIONS,POST | `cancel_ewb` | AllowAny 🔴 | Body: { "ewbNo": 123, "reason_code": 2, "remarks": "..." }. Updates the stored record. |
| `/api/ewaybill/close/` | OPTIONS,POST | `close_ewb` | AllowAny 🔴 | Voluntary closure of an EWB after delivery (GSTN advisory 17.06.2026). |
| `/api/ewaybill/extend-validity/` | OPTIONS,POST | `extend_validity` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/ewaybill/from-invoice/<int:docentry>/` | GET,OPTIONS,POST | `ewb_from_invoice` | AllowAny 🔴 | Build an e-Way Bill straight from a SAP B1 invoice (OINV DocEntry). |
| `/api/ewaybill/generate/` | OPTIONS,POST | `generate_ewb` | AllowAny 🔴 | Body: full GENEWAYBILL payload (supply + transport details). |
| `/api/ewaybill/gstin/<str:gstin>/` | GET,OPTIONS | `ewb_gstin_details` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/ewaybill/reject/` | OPTIONS,POST | `reject_ewb` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/ewaybill/token/` | OPTIONS,POST | `ewb_token` | AllowAny 🔴 | Run the EWB auth handshake; returns a masked token. |
| `/api/ewaybill/transporter/<str:trans_id>/` | GET,OPTIONS | `ewb_transporter_details` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/ewaybill/update-part-b/` | OPTIONS,POST | `update_part_b` | AllowAny 🔴 | Body: VEHEWB payload (ewbNo, vehicleNo, fromPlace, fromState, transMode, ...). |
| `/api/ewaybill/update-transporter/` | OPTIONS,POST | `update_transporter` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |

## `hana` — 23 routes, **23 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/hana/address/` | GET,OPTIONS | `GetAddressView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/all-customers/` | GET,OPTIONS | `GetAllCustomersView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/batch-details/` | GET,OPTIONS | `GetBatchDetailsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/customer-details/` | GET,OPTIONS | `GetCustomerDetailsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/draft/verify` | GET,OPTIONS | `GetDraftVerification` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/fg-items/` | GET,OPTIONS | `GetFGItemsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/freight-masters/` | GET,OPTIONS | `GetFreightMastersView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/inventory-details/` | GET,OPTIONS | `GetInventoryDetailsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/inventory-report/` | GET,OPTIONS | `GetInventoryReportView` | AllowAny 🔴 | Warehouse-wise FG stock, pivoted and grouped by variety. |
| `/api/hana/invoice-drafts/` | GET,OPTIONS | `GetInvoiceDrafts` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/item-price/` | GET,OPTIONS | `GetItemPriceView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/next-doc-number/` | GET,OPTIONS | `GetNextDocNumberView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/open-parties/` | GET,OPTIONS | `GetOpenPartiesView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/pending-dispatch/` | GET,OPTIONS | `GetPendingDispatchView` | AllowAny 🔴 | Open sales orders against the AR invoices raised on them. |
| `/api/hana/product-so/` | GET,OPTIONS | `GetProductSalesOrderView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/product-stock/` | GET,OPTIONS | `GetProductStockView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/salesperson-details/` | GET,OPTIONS | `GetSalespersonDetailsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/series/` | GET,OPTIONS | `GetSeries` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/so/` | GET,OPTIONS | `GetSalesOrderView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/state-chain/` | GET,OPTIONS | `GetStateChainView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/vendor-states/` | GET,OPTIONS | `GetVendorStatesView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/warehouse-details/` | GET,OPTIONS | `GetWarehouseDetailsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/hana/warehouses/` | GET,OPTIONS | `GetWarehousesView` | AllowAny 🔴 | Selectable warehouses, for the order-level warehouse picker. |

## `invoice` — 14 routes, **12 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/invoice/<int:pk>/delete/` | DELETE,OPTIONS,POST | `InvoiceLogDeleteView` | AllowAny 🔴 | Soft-delete a review entry, and restore one. |
| `/api/invoice/<int:pk>/update-status/` | OPTIONS,PATCH | `InvoicelogStatusUpdateView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/all/` | GET,OPTIONS | `InvoiceLogListView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/credit-limit/cards/` | GET,OPTIONS | `CreditLimitCardsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/credit-limit/flow/` | GET,OPTIONS | `GetCreditLimitJSAPFlow` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/credit-limit/request/` | OPTIONS,POST | `CreditLimitRequestView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/crystal/` | GET,OPTIONS | `GetPrintReport` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/history/<int:pk>/` | GET,OPTIONS | `InvoiceHistoryView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/log/<int:id>/` | GET,OPTIONS,PATCH,PUT | `UpdateInvoiceLogView` | AllowAny 🔴 | Concrete view for retrieving, updating a model instance. |
| `/api/invoice/logs/all/` | GET,OPTIONS | `InvoiceLogListwoWhsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/pending/` | OPTIONS,POST | `InvoiceLogCreateView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/invoice/refLogs/` | OPTIONS,POST | `InvoiceRefLogCreateView` | AllowAny 🔴 | Concrete view for creating a model instance. |
| `/api/invoice/reserved-batches/` | GET,OPTIONS | `ReservedBatchesView` | IsAuthenticated | How much of each batch an in-flight invoice log has already committed. |
| `/api/invoice/used-sales-orders/` | GET,OPTIONS | `UsedSalesOrdersView` | IsAuthenticated | Which sales orders already appear on an invoice log. |

## `legal` — 8 routes, **8 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/legal/item-nutrition/` | GET,OPTIONS | `NutrientByItemView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/legal/item/` | GET,OPTIONS,POST | `LabelItemListCreateView` | AllowAny 🔴 | Concrete view for listing a queryset or creating a model instance. |
| `/api/legal/item/<int:id>/` | DELETE,GET,OPTIONS,PATCH,PUT | `LabelItemRetrieveUpdateDestroyView` | AllowAny 🔴 | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/legal/nutrition/` | GET,OPTIONS,POST | `LabelNutritionListCreateView` | AllowAny 🔴 | Concrete view for listing a queryset or creating a model instance. |
| `/api/legal/nutrition/<int:id>/` | DELETE,GET,OPTIONS,PATCH,PUT | `LabelNutritionRetrieveUpdateDestroyView` | AllowAny 🔴 | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/legal/uom/` | GET,OPTIONS,POST | `NutritionUOMListCreatView` | AllowAny 🔴 | Concrete view for listing a queryset or creating a model instance. |
| `/api/legal/uom/<int:id>/` | DELETE,GET,OPTIONS,PATCH,PUT | `NutritionUOMRetrieveUpdateDestroyView` | AllowAny 🔴 | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/legal/upload/` | OPTIONS,POST | `FeedtoAIView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |

## `notifications` — 3 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/notifications/` | GET,OPTIONS,PATCH,POST | `FrameworkNotificationListView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/notifications/<int:pk>/` | GET,OPTIONS,PATCH,POST | `FrameworkNotificationListView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/notifications/unread-count/` | GET,OPTIONS | `FrameworkNotificationUnreadCountView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |

## `orders` — 55 routes, **24 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/orders/<int:order_id>/approve/` | OPTIONS,POST | `ApproveOrderView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/<int:order_id>/cancel-quotation/` | OPTIONS,POST | `CancelSalesQuotationView` | IsAuthenticated | Cancel a completed order's SAP Sales Quotation, then mirror it in OMS. |
| `/api/orders/<int:order_id>/orderdetails/` | GET,OPTIONS | `OrderDetailsByOrderView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/<int:order_id>/orderlogs/` | GET,OPTIONS | `OrderLogsByOrderView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/<int:order_id>/reject/` | OPTIONS,POST | `RejectOrderView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/<int:order_id>/update-status/` | OPTIONS,POST | `UpdateOrderStatusView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/<int:order_id>/update/` | OPTIONS,PUT | `UpdateOrderView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/addresses/` | GET,OPTIONS | `PartyAddressesView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/api/ai-order-summary/` | ? | `ai_order_summary` | (none declared) 🔴 |  |
| `/api/orders/branch/` | GET,OPTIONS | `BranchView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/create-scheme/` | OPTIONS,POST | `CreateSchemeView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/create/` | OPTIONS,POST | `CreateOrderView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/dashboard/` | GET,OPTIONS | `DashboardKPIView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/dashboard/charts/` | GET,OPTIONS | `DashboardChartsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/dashboardW/` | GET,OPTIONS | `WDashboardKPIView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/dashboardW/charts/` | GET,OPTIONS | `WDashboardChartsView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/dispatches/` | GET,OPTIONS | `DispatchLocationListView` | AllowAny 🔴 | Concrete view for listing a queryset. |
| `/api/orders/flow-config/` | GET,OPTIONS,POST | `OrderFlowConfigView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/list/` | GET,OPTIONS | `OrderListView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/mart/<int:order_id>/` | GET,OPTIONS | `MartOrderDetailView` | IsAuthenticated | One distributor order with its items, for the approver's edit screen. |
| `/api/orders/mart/<int:order_id>/approve/` | OPTIONS,POST | `MartApproveView` | IsAuthenticated | Approve a distributor order → 'Mart Approved'. (Phase 2 will also push the |
| `/api/orders/mart/<int:order_id>/reject/` | OPTIONS,POST | `MartRejectView` | IsAuthenticated | Reject a distributor order with a mandatory reason → 'Mart Rejected'. |
| `/api/orders/mart/<int:order_id>/resend-sap/` | OPTIONS,POST | `MartResendSapView` | IsAuthenticated | Retry pushing an already-approved distributor order to SAP as a Sales |
| `/api/orders/mart/list/` | GET,OPTIONS | `MartOrderListView` | IsAuthenticated | All distributor (company 3 / Mart) orders, for the Mart Approval queue. |
| `/api/orders/notifications/` | GET,OPTIONS,PATCH,POST | `NotificationListView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/notifications/<int:pk>/` | GET,OPTIONS,PATCH,POST | `NotificationListView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/notifications/history/` | GET,OPTIONS | `NotificationHistoryView` | IsAuthenticated | Paginated notification history (Phase 3, Task 9). |
| `/api/orders/orderdetailsbyid/<int:order_id>/` | GET,OPTIONS | `OrderDetailsByOrderView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/orders-by-item/` | GET,OPTIONS | `GetOrdersByItemView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/ordersbyuser/<int:user_id>/` | GET,OPTIONS | `OrdersByUserView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/parties/` | GET,OPTIONS | `PartyView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/party-flow-config/` | DELETE,GET,OPTIONS,POST | `PartyOrderFlowConfigView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/party-products/<str:card_code>/` | GET,OPTIONS | `PartyProductsView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/product-filters/` | GET,OPTIONS | `ProductFiltersView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/products/` | GET,OPTIONS | `ProductListView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/push-token/` | DELETE,OPTIONS,POST | `PushTokenView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/quotation-overview/` | GET,OPTIONS | `QuotationOverviewView` | IsAuthenticated | Admin overview of every completed order and its SAP sales-quotation |
| `/api/orders/quotation-status/` | GET,OPTIONS | `QuotationStatusView` | AllowAny 🔴 | Batch lookup of SAP Sales Quotation status for completed orders. |
| `/api/orders/sales-order-status/` | GET,OPTIONS | `SalesOrderSapStatusView` | IsAuthenticated | Batch lookup of SAP Sales Order status for distributor orders. |
| `/api/orders/schemes/` | GET,OPTIONS | `SchemeListView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/schemes/<int:scheme_id>/` | DELETE,GET,OPTIONS,PATCH,PUT | `SchemeDetailView` | AllowAny 🔴 | Read / update / delete a single scheme. |
| `/api/orders/schemes/manage/` | GET,OPTIONS | `SchemeManageListView` | AllowAny 🔴 | Full scheme rows for the Add Scheme management table. |
| `/api/orders/staff-products/` | GET,OPTIONS,POST | `StaffProductsAPIView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/status-tracking/` | GET,OPTIONS | `OrderStatusTrackingView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/status/` | GET,OPTIONS | `OrderStatusList` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/stock-check/` | GET,OPTIONS | `OrderStockCheckView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/templates/orders/` | GET,OPTIONS | `TemplateOrderListView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/templates/parties/` | GET,OPTIONS | `TemplatePartyListView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/orders/v2/schemes/` | GET,OPTIONS,POST | `SchemeV2ListCreateView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/v2/schemes/<int:scheme_id>/` | DELETE,GET,OPTIONS,PATCH | `SchemeV2DetailView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/orders/v2/schemes/<int:scheme_id>/assignments/` | DELETE,GET,OPTIONS,POST | `SchemeAssignmentView` | AllowAny 🔴 | Target a scheme at a party, a state, a main group, a category, or everyone. |
| `/api/orders/v2/schemes/applicable/` | GET,OPTIONS | `SchemeApplicableView` | AllowAny 🔴 | Everything reaching a vendor, with the scope that let each scheme in -- |
| `/api/orders/v2/schemes/preview/` | OPTIONS,POST | `SchemePreviewView` | AllowAny 🔴 | Dry-run the engine over a draft order. |
| `/api/orders/web-push/public-key/` | GET,OPTIONS | `WebPushPublicKeyView` | IsAuthenticated | Expose the VAPID public (application server) key the browser needs to |
| `/api/orders/web-push/subscribe/` | DELETE,OPTIONS,POST | `WebPushSubscriptionView` | IsAuthenticated | Register or remove a browser Web Push subscription for the caller. |

## `payments` — 29 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/payments/admin/collection-persons/` | GET,OPTIONS,POST | `CollectionPersonAdminListCreateView` | IsAuthenticated,IsApprovalAdmin | Admin CRUD for the "Received From" people. |
| `/api/payments/admin/collection-persons/<int:pk>/` | DELETE,GET,OPTIONS,PATCH,PUT | `CollectionPersonAdminDetailView` | IsAuthenticated,IsApprovalAdmin | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/payments/admin/method-mapping-status/` | GET,OPTIONS | `PaymentMethodMappingStatusView` | IsAuthenticated,IsApprovalAdmin | One row per payment method: what it maps to, and whether SAP still has it. |
| `/api/payments/admin/method-mappings/` | GET,OPTIONS,POST | `PaymentMethodMappingAdminView` | IsAuthenticated,IsApprovalAdmin | Admin CRUD for payment method -> SAP account mapping. |
| `/api/payments/admin/method-mappings/<int:pk>/` | DELETE,GET,OPTIONS,PATCH,PUT | `PaymentMethodMappingAdminDetailView` | IsAuthenticated,IsApprovalAdmin | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/payments/bank-accounts/` | GET,OPTIONS | `BankAccountListView` | IsAuthenticated | Bank accounts for a company, live from SAP House Bank Accounts. |
| `/api/payments/banks/` | GET,OPTIONS | `BankAccountListView` | IsAuthenticated | Bank accounts for a company, live from SAP House Bank Accounts. |
| `/api/payments/collection-persons/` | GET,OPTIONS | `CollectionPersonListView` | IsAuthenticated | The manually-maintained "Received From" people. |
| `/api/payments/companies/` | GET,OPTIONS | `CompanyListView` | IsAuthenticated | Step 1. Companies (SAP categories) available to the caller. |
| `/api/payments/company-mappings/` | GET,OPTIONS,POST | `CompanyMappingListCreateView` | IsAuthenticated,IsApprovalAdmin | Admin CRUD for company -> SAP database mappings. |
| `/api/payments/company-mappings/<int:pk>/` | DELETE,GET,OPTIONS,PATCH,PUT | `CompanyMappingDetailView` | IsAuthenticated,IsApprovalAdmin | Concrete view for retrieving, updating or deleting a model instance. |
| `/api/payments/dashboard/` | GET,OPTIONS | `PaymentDashboardView` | IsAuthenticated,CanViewPaymentsDashboard | Every figure on the Payments Dashboard, in one response. |
| `/api/payments/dashboard/collection-performance/` | GET,OPTIONS | `CollectionPerformanceView` | IsAuthenticated,CanViewPaymentsDashboard | Just the participants table — for paging, searching and sorting. |
| `/api/payments/dashboard/person/<str:kind>/<int:pk>/` | GET,OPTIONS | `PersonAnalyticsView` | IsAuthenticated,CanViewPaymentsDashboard | One participant's collection history. |
| `/api/payments/depositable-receipts/` | GET,OPTIONS | `DepositableReceiptListView` | IsAuthenticated | Posted CASH/CHEQUE receipts in this company not yet banked. |
| `/api/payments/deposits/` | GET,OPTIONS,POST | `BankDepositListCreateView` | IsAuthenticated,ReadOrCreateDeposit | Intentionally simple parent class for all views. Only implements |
| `/api/payments/deposits/<int:pk>/` | GET,OPTIONS,PATCH | `BankDepositDetailView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/payments/deposits/<int:pk>/attachments/` | OPTIONS,POST | `DepositAttachmentUploadView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/payments/deposits/<int:pk>/submit/` | OPTIONS,POST | `BankDepositSubmitView` | IsAuthenticated,CanCreateDeposit | Intentionally simple parent class for all views. Only implements |
| `/api/payments/my-permissions/` | GET,OPTIONS | `MyPaymentPermissionsView` | IsAuthenticated | What the CALLER may do in this module. |
| `/api/payments/open-invoices/` | GET,OPTIONS | `OpenInvoiceListView` | IsAuthenticated | Step 3. LIVE open invoices for a party. |
| `/api/payments/parties/` | GET,OPTIONS | `PartyListView` | IsAuthenticated | Step 2. Parties for a company. EVERY party, not a per-user subset. |
| `/api/payments/receipts/` | GET,OPTIONS,POST | `PaymentReceiptListCreateView` | IsAuthenticated,ReadOrCreatePayment | Intentionally simple parent class for all views. Only implements |
| `/api/payments/receipts/<int:pk>/` | GET,OPTIONS,PATCH | `PaymentReceiptDetailView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/payments/receipts/<int:pk>/attachments/` | OPTIONS,POST | `ReceiptAttachmentUploadView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/payments/receipts/<int:pk>/history/` | GET,OPTIONS | `PaymentReceiptHistoryView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/payments/receipts/<int:pk>/sap-report/` | GET,OPTIONS | `SapReceiptPdfView` | IsAuthenticated | Stream an OMS-generated, SAP-style receipt PDF for one posted receipt. |
| `/api/payments/receipts/<int:pk>/submit/` | OPTIONS,POST | `PaymentReceiptSubmitView` | IsAuthenticated,CanCreatePayment | Send a draft (or rejected) receipt into the approval chain. |
| `/api/payments/sap-branches/` | GET,OPTIONS | `SapBranchListView` | IsAuthenticated | SAP branches a payment may be posted to, for this company. |

## `sap_sync` — 27 routes, **26 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/sap/addresses/` | GET,OPTIONS | `PartyAddressListView` | AllowAny 🔴 | List all party addresses with optional filter |
| `/api/sap/approve-order/` | OPTIONS,POST | `ApproveOrderAPIView` | AllowAny 🔴 | Approve an order and push to SAP |
| `/api/sap/approve-sales-order/` | OPTIONS,POST | `ApproveSalesOrderAPIView` | AllowAny 🔴 | Approve an order and push to SAP |
| `/api/sap/branches/` | GET,OPTIONS | `BranchListView` | AllowAny 🔴 | Get all branches |
| `/api/sap/logs/` | GET,OPTIONS | `SyncLogListView` | AllowAny 🔴 | List all sync logs |
| `/api/sap/parties/` | GET,OPTIONS | `PartyListView` | AllowAny 🔴 | Concrete view for listing a queryset. |
| `/api/sap/parties/<int:pk>/` | GET,OPTIONS | `PartyDetailView` | AllowAny 🔴 | Get single party with addresses |
| `/api/sap/parties/category/` | GET,OPTIONS | `GetPartyByCategoryView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/sap/parties/code/<str:card_code>/` | GET,OPTIONS | `PartyByCodeView` | AllowAny 🔴 | Get party by card_code with addresses |
| `/api/sap/product-varieties/` | GET,OPTIONS | `ProductVarietyListView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/sap/products/` | GET,OPTIONS | `ProductListView` | AllowAny 🔴 | Concrete view for listing a queryset. |
| `/api/sap/products/<int:pk>/` | GET,OPTIONS | `ProductDetailView` | AllowAny 🔴 | Get single product by ID or item_code |
| `/api/sap/products/code/<str:item_code>/` | GET,OPTIONS | `ProductByCodeView` | AllowAny 🔴 | Get product by item_code |
| `/api/sap/push-order/` | OPTIONS,POST | `PushSalesOrderView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/sap/push-quotation/` | OPTIONS,POST | `PushSalesQuotationView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/sap/quotation-log/<int:order_id>/` | GET,OPTIONS | `SalesQuotationLogByOrderView` | IsAuthenticated | Get the latest successful SAP quotation log for an order. |
| `/api/sap/schedules/` | GET,OPTIONS,POST | `SyncScheduleListView` | AllowAny 🔴 | List and create sync schedules |
| `/api/sap/schedules/<int:pk>/` | DELETE,GET,OPTIONS,PUT | `SyncScheduleDetailView` | AllowAny 🔴 | Get, update, or delete a sync schedule |
| `/api/sap/schedules/<int:pk>/toggle/` | OPTIONS,POST | `ToggleScheduleView` | AllowAny 🔴 | Activate or deactivate a schedule |
| `/api/sap/status/` | GET,OPTIONS | `SyncStatusView` | AllowAny 🔴 | Get sync status with counts |
| `/api/sap/sync/addresses/` | OPTIONS,POST | `SyncPartyAddressesView` | AllowAny 🔴 | Trigger manual sync of party addresses only |
| `/api/sap/sync/all/` | OPTIONS,POST | `SyncAllView` | AllowAny 🔴 | Trigger manual sync of all data (Products, Parties, Addresses) |
| `/api/sap/sync/branches/` | OPTIONS,POST | `SyncBranchesView` | AllowAny 🔴 | Sync branches from SAP |
| `/api/sap/sync/parties/` | OPTIONS,POST | `SyncPartiesView` | AllowAny 🔴 | Trigger manual sync of parties only |
| `/api/sap/sync/products/` | OPTIONS,POST | `SyncProductsView` | AllowAny 🔴 | Trigger manual sync of products only |
| `/api/sap/test-quotation/` | OPTIONS,POST | `TestSalesQuotation` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/sap/test-quotation/<int:pk>/` | OPTIONS,POST | `TestSalesQuotation` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |

## `serviceLayer` — 7 routes, **3 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/service-layer/ap/grpo/` | GET,OPTIONS | `GRPODetailView` | IsTrackerAP | GET /api/service-layer/ap/grpo/?branch=OIL&doc_entry=25773  (or &doc_num=) |
| `/api/service-layer/ap/invoice/` | OPTIONS,POST | `APInvoiceCreateView` | IsTrackerAP | POST /api/service-layer/ap/invoice/?branch=OIL |
| `/api/service-layer/ap/open-grpos/` | GET,OPTIONS | `OpenGRPOListView` | IsTrackerAP | GET /api/service-layer/ap/open-grpos/?branch=OIL[&vendor=CARD][&search=] |
| `/api/service-layer/ap/vendor-tds/` | GET,OPTIONS | `VendorTDSView` | IsTrackerAP | GET /api/service-layer/ap/vendor-tds/?branch=OIL&card_code=VENDA001320 |
| `/api/service-layer/draft-action/` | OPTIONS,POST | `DraftActionView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/service-layer/draft/` | GET,OPTIONS,POST | `DraftView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/service-layer/invoice/` | OPTIONS,POST | `SAPInvoiceCreateView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |

## `tracker` — 24 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/tracker/actions/bulk/` | OPTIONS,POST | `BulkActionView` | IsTrackerUser | Advance / return / hold one or many invoices at once. |
| `/api/tracker/admin/lookups/<str:kind>/` | GET,OPTIONS,POST | `LookupAdminListCreate` | IsTrackerAdmin | Intentionally simple parent class for all views. Only implements |
| `/api/tracker/admin/lookups/<str:kind>/<int:pk>/` | DELETE,OPTIONS,PATCH | `LookupAdminDetail` | IsTrackerAdmin | Intentionally simple parent class for all views. Only implements |
| `/api/tracker/admin/stages/` | GET,OPTIONS,POST | `StageAdminListCreate` | IsTrackerAdmin | Intentionally simple parent class for all views. Only implements |
| `/api/tracker/admin/stages/<int:pk>/` | DELETE,OPTIONS,PATCH | `StageAdminDetail` | IsTrackerAdmin | Intentionally simple parent class for all views. Only implements |
| `/api/tracker/admin/tracker-users/` | GET,OPTIONS,POST | `TrackerUserListCreate` | IsTrackerAdmin | List all tracker users, or create a new one under a tracker sub-role. |
| `/api/tracker/admin/tracker-users/<int:user_id>/` | DELETE,OPTIONS,PATCH | `TrackerUserDetail` | IsTrackerAdmin | Edit or delete a tracker user. Refuses to touch non-tracker users. |
| `/api/tracker/admin/users/` | GET,OPTIONS | `UserStageListView` | IsTrackerAdmin | List users with the stage ids they're currently assigned to. |
| `/api/tracker/admin/users/<int:user_id>/stages/` | OPTIONS,PUT | `UserStageSetView` | IsTrackerAdmin | Replace a user's full set of stage assignments with `stage_ids`. |
| `/api/tracker/alerts/` | GET,OPTIONS | `AlertsView` | IsTrackerAlerts | Active stuck-invoice alerts, scoped to the stages the user handles. |
| `/api/tracker/all-invoices/` | GET,OPTIONS | `AdminInvoicesView` | IsTrackerAdmin | Master list of EVERY invoice for the tracker admin, with filters. |
| `/api/tracker/all-invoices/export/` | GET,OPTIONS | `AdminInvoicesExportView` | IsTrackerAdmin | Excel export of every invoice in the office's original sheet layout, |
| `/api/tracker/invoices/` | GET,OPTIONS,POST | `InvoiceListCreateView` | IsTrackerEntry | Intentionally simple parent class for all views. Only implements |
| `/api/tracker/invoices/<int:pk>/` | DELETE,GET,OPTIONS,PATCH | `InvoiceDetailView` | IsTrackerUser | Intentionally simple parent class for all views. Only implements |
| `/api/tracker/invoices/<int:pk>/jsap/` | GET,OPTIONS | `JsapStatusView` | IsTrackerUser | Budget-approval status of one invoice, straight from JSAP. |
| `/api/tracker/invoices/<int:pk>/payment/` | OPTIONS,PATCH | `PaymentDetailView` | IsTrackerUser | Capture / update payment at the terminal stage; marking it Paid |
| `/api/tracker/jsap/sync/` | OPTIONS,POST | `JsapSyncView` | IsTrackerUser | Manual "refresh from JSAP" for the JSAP desk. |
| `/api/tracker/lookups/` | GET,OPTIONS | `LookupsView` | IsTrackerUser | Everything the entry form / filters need in one call. |
| `/api/tracker/my-queue/` | GET,OPTIONS | `MyQueueView` | IsTrackerUser | The actionable inbox: invoices parked at a stage this user handles. |
| `/api/tracker/reports/` | GET,OPTIONS | `ReportsView` | IsTrackerReports | Turnaround analytics: pending, avg days/stage, bottlenecks, ageing. |
| `/api/tracker/stage-advanced/` | GET,OPTIONS | `StageAdvancedView` | IsTrackerUser | Invoices already advanced FORWARD from a stage (read-only history). |
| `/api/tracker/stage-decisions/` | GET,OPTIONS | `StageDecisionsView` | IsTrackerUser | The decision log of a stage: what this desk decided, and what became of it. |
| `/api/tracker/stage-export/` | GET,OPTIONS | `StageExportView` | IsTrackerUser | Excel export of one queue tab, in the SAME register layout as the |
| `/api/tracker/vendors/` | GET,OPTIONS | `VendorsView` | IsTrackerEntry | SAP vendors for the entry form's searchable party dropdown (fast, |

## `uilabels` — 4 routes, **0 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/ui-config/admin/labels/` | GET,OPTIONS,POST | `AdminLabelListCreateView` | IsAuthenticated,IsAdminRole | List every label (active or not) and create new ones. Admin only. |
| `/api/ui-config/admin/labels/<int:pk>/` | DELETE,GET,OPTIONS,PUT | `AdminLabelDetailView` | IsAuthenticated,IsAdminRole | Retrieve / update / delete a single label. Admin only. |
| `/api/ui-config/fields/` | GET,OPTIONS | `PublicFieldsView` | IsAuthenticated | Field-behaviour config for input fields (as opposed to plain labels). |
| `/api/ui-config/labels/` | GET,OPTIONS | `PublicLabelsView` | IsAuthenticated | Flat ``{field_key: display_name}`` map of every ACTIVE label. |

## `users` — 26 routes, **16 open**

| path | methods | view | permissions | note |
|---|---|---|---|---|
| `/api/auth/assign-parties/` | OPTIONS,POST | `AssignPartiesView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/assign-parties/bulk-upload/` | OPTIONS,POST | `BulkAssignUsersPartiesView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/bulk-party/assign-products/` | OPTIONS,POST | `BulkAssignPartyToProductView` | IsAuthenticated | Assign multiple products to multiple parties |
| `/api/auth/categories/` | GET,OPTIONS | `CategoryListView` | AllowAny 🔴 | Get all categories |
| `/api/auth/combo-mappings/` | GET,OPTIONS,POST | `ComboMappingsView` | IsAuthenticated | Combo packs and the free-of-cost item each one carries. |
| `/api/auth/companies/` | GET,OPTIONS | `CompanyListView` | AllowAny 🔴 | Get all active companies |
| `/api/auth/login/` | OPTIONS,POST | `LoginView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/logout/` | OPTIONS,POST | `LogoutView` | IsAuthenticated | POST /api/auth/logout/ — blacklist the refresh token so it cannot be |
| `/api/auth/mainGroup/` | GET,OPTIONS | `MainGroupListView` | AllowAny 🔴 | Get all active main groups |
| `/api/auth/parties/<str:card_code>/products/` | GET,OPTIONS | `PartyProductsView` | IsAuthenticated | Get all products assigned to a party with their basic_rate |
| `/api/auth/parties/<str:card_code>/users/` | GET,OPTIONS | `PartyUsersView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/party-product/add/` | OPTIONS,POST | `AssignProductToPartyView` | IsAuthenticated | Add single product to party with basic_rate |
| `/api/auth/party-product/bulk-add/` | OPTIONS,POST | `BulkAssignProductsToPartyView` | IsAuthenticated | Add multiple products to a party |
| `/api/auth/party-product/remove/` | OPTIONS,POST | `RemoveProductFromPartyView` | IsAuthenticated | Remove a product from party |
| `/api/auth/party-product/update-rate/` | OPTIONS,POST | `UpdateProductRateView` | IsAuthenticated | Update basic_rate for a party-product |
| `/api/auth/profile/` | GET,OPTIONS | `ProfileView` | IsAuthenticated | Intentionally simple parent class for all views. Only implements |
| `/api/auth/refresh/` | OPTIONS,POST | `AuthTokenRefreshView` | AllowAny 🔴 | POST /api/auth/refresh/ — exchange a refresh token for a new access |
| `/api/auth/remove-party/` | OPTIONS,POST | `RemovePartyAssignmentView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/roles/` | GET,OPTIONS | `RoleListView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/states/` | GET,OPTIONS | `StateListView` | AllowAny 🔴 | Get all active states |
| `/api/auth/users/<int:user_id>/` | GET,OPTIONS,PUT | `UserDetailView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/users/<int:user_id>/delete/` | OPTIONS,POST | `DeleteUserView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/users/<int:user_id>/page-permissions/` | GET,OPTIONS,PUT | `PagePermissionsView` | IsAuthenticated | Admin-managed per-user page access (list of page keys). |
| `/api/auth/users/<int:user_id>/parties/` | GET,OPTIONS | `UserPartiesView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/users/create/` | OPTIONS,POST | `CreateUserView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |
| `/api/auth/users/list/` | GET,OPTIONS | `UserListForAssignmentView` | AllowAny 🔴 | Intentionally simple parent class for all views. Only implements |

---

**Totals: 294 routes, 149 reachable without authentication.**
