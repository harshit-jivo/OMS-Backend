# Data model reference

Generated from the Django model registry. `unmanaged` means Django will not create or migrate the table.


## `HAIS` — 6 models

### `Asset` → `hais"."tbl_Asset`  ordering=['-created_at']

> One physical device. Its Asset ID is the primary key.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `asset_id` | CharField |  |  | unique; len 40 |
| `serial_num` | CharField |  |  | unique; len 120 |
| `qr_code` | CharField |  |  | len 160; default='' |
| `asset_type` | ForeignKey | Y | Y | → `HAIS.AssetType` PROTECT |
| `company` | CharField |  |  | len 100; default='' |
| `model_num` | CharField |  |  | len 100; default='' |
| `warranty_ends` | CharField |  |  | len 20; default='' |
| `processor` | CharField |  |  | len 120; default='' |
| `memory` | CharField |  |  | len 60; default='' |
| `operating_system` | CharField |  |  | len 80; default='' |
| `storage` | CharField |  |  | len 60; default='' |
| `current_user_id` | CharField |  |  | len 40; default='' |
| `current_user_name` | CharField |  |  | len 120; default='' |
| `prev_user_id` | CharField |  |  | len 40; default='' |
| `prev_user_name` | CharField |  |  | len 120; default='' |
| `department` | ForeignKey | Y | Y | → `HAIS.Department` PROTECT |
| `email_id` | CharField |  |  | len 120; default='' |
| `current_location` | CharField |  |  | len 160; default='' |
| `handover_date` | CharField |  |  | len 20; default='' |
| `purchase_invoice_no` | CharField |  |  | len 60; default='' |
| `purchase_invoice_date` | CharField |  |  | len 20; default='' |
| `amount` | DecimalField | Y |  |  |
| `vendor` | CharField |  |  | len 120; default='' |
| `date_of_last_service` | CharField |  |  | len 20; default='' |
| `working_status` | CharField |  |  | choices: Working, Under Repair, Not Working, In Stock, Scrapped; len 20; default='Working' |
| `remarks` | TextField |  |  | default='' |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |
| `storage_types` | ManyToManyField |  |  | → `HAIS.StorageType` |

- **index** `hais_asset_serial_idx`: ['serial_num']
- **index** `hais_asset_curuser_idx`: ['current_user_id']
- **index** `hais_asset_status_idx`: ['working_status']

### `AssetLog` → `hais"."tbl_AssetLog`  ordering=['asset_id', 'id']

> One event in a device's life. Append-only; never updated after write.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `asset` | ForeignKey |  | Y | → `HAIS.Asset` CASCADE |
| `action` | CharField |  |  | len 40 |
| `event_date` | CharField |  |  | len 20; default='' |
| `from_user_id` | CharField |  |  | len 40; default='' |
| `from_user_name` | CharField |  |  | len 120; default='' |
| `to_user_id` | CharField |  |  | len 40; default='' |
| `to_user_name` | CharField |  |  | len 120; default='' |
| `department` | ForeignKey | Y | Y | → `HAIS.Department` SET_NULL |
| `location` | CharField |  |  | len 160; default='' |
| `reason` | CharField |  |  | len 255; default='' |
| `config_json` | JSONField |  |  | default=<class 'dict'> |
| `config_change` | CharField |  |  | len 255; default='' |
| `created_at` | DateTimeField |  |  |  |
| `created_by` | CharField |  |  | len 120; default='' |

- **index** `hais_log_asset_idx`: ['asset', 'id']

### `AssetStorageType` → `hais"."tbl_AssetStorageType`

> Link row: one storage type on one device (the multi-select).


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `asset` | ForeignKey |  | Y | → `HAIS.Asset` CASCADE |
| `storage_type` | ForeignKey |  | Y | → `HAIS.StorageType` PROTECT |

- **constraint** `hais_asset_storagetype_uq`: UniqueConstraint

### `AssetType` → `hais"."tbl_AssetType`  ordering=['sort_order', 'name']

> Asset Type / Category, e.g. Laptop, Desktop, Monitor.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `sort_order` | PositiveSmallIntegerField |  |  | default=0 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |
| `id_prefix` | CharField |  |  | len 8; default='' |

### `Department` → `hais"."tbl_Departments`  ordering=['sort_order', 'name']

> Owning department, e.g. IT, Accounts, Admin.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `sort_order` | PositiveSmallIntegerField |  |  | default=0 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

### `StorageType` → `hais"."tbl_StorageType`  ordering=['sort_order', 'name']

> Storage medium, e.g. SSD, NVMe SSD, HDD. Multi-select on the device.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `sort_order` | PositiveSmallIntegerField |  |  | default=0 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |


## `SKU` — 1 models

### `SKU` → `sku`  ordering=['-uploaded_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `item_code` | CharField |  |  | unique; len 255 |
| `item_name` | CharField |  |  | len 255 |
| `item_image` | FileField |  |  | len 100 |
| `uploaded_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |


## `approvals` — 5 models

### `ApprovalAction` → `approval_action`  ordering=['request_id', 'sequence']

> Append-only decision log.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `request` | ForeignKey |  | Y | → `approvals.ApprovalRequest` CASCADE |
| `sequence` | PositiveIntegerField |  |  |  |
| `round_number` | PositiveSmallIntegerField |  |  | default=1 |
| `level` | PositiveSmallIntegerField |  |  | default=0 |
| `level_name` | CharField |  |  | len 60; default='' |
| `action` | CharField |  |  | choices: SUBMIT, APPROVE, REJECT, CANCEL, RESUBMIT; len 12 |
| `remarks` | TextField |  |  | default='' |
| `approver` | ForeignKey |  | Y | → `users.User` PROTECT |
| `approver_username` | CharField |  |  | len 150; default='' |
| `approver_role` | CharField |  |  | len 50; default='' |
| `ip_address` | GenericIPAddressField | Y |  | len 39 |
| `user_agent` | CharField |  |  | len 400; default='' |
| `acted_at` | DateTimeField |  | Y |  |

- **constraint** `approval_action_seq_uq`: UniqueConstraint
- **index** `idx_apact_user`: ['approver', '-acted_at']

### `ApprovalLevel` → `approval_level`  ordering=['workflow_id', 'sequence']

> One rung. `sequence` is the ordering column OrderFlowConfig lacks.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `workflow` | ForeignKey |  | Y | → `approvals.ApprovalWorkflow` CASCADE |
| `sequence` | PositiveSmallIntegerField |  |  |  |
| `name` | CharField |  |  | len 60 |
| `role` | ForeignKey | Y | Y | → `users.UserRole` PROTECT |
| `min_approvals` | PositiveSmallIntegerField |  |  | default=1 |
| `is_active` | BooleanField |  |  | default=True |

- **constraint** `approval_level_seq_uq`: UniqueConstraint

### `ApprovalLevelApprover` → `approval_level_approver`

> Explicit user grant at a level — the port of tracker.UserStageAccess.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `level` | ForeignKey |  | Y | → `approvals.ApprovalLevel` CASCADE |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `company` | CharField |  |  | choices: OIL, BEVERAGES, MART; len 20; default='' |
| `is_active` | BooleanField |  |  | default=True |
| `assigned_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `assigned_at` | DateTimeField |  |  |  |

- **constraint** `approval_level_approver_uq`: UniqueConstraint
- **index** `idx_ala_user_active`: ['user', 'is_active']
- **index** `idx_ala_level_active`: ['level', 'is_active']

### `ApprovalRequest` → `approval_request`  ordering=['-created_at']

> The live approval state of ONE document, attached generically.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `workflow` | ForeignKey |  | Y | → `approvals.ApprovalWorkflow` PROTECT |
| `content_type` | ForeignKey |  | Y | → `contenttypes.ContentType` PROTECT |
| `object_id` | PositiveBigIntegerField |  |  |  |
| `company` | CharField |  | Y | choices: OIL, BEVERAGES, MART; len 20 |
| `amount` | DecimalField |  |  | default=0 |
| `document_number` | CharField |  |  | len 50; default='' |
| `status` | CharField |  | Y | choices: DRAFT, PENDING, APPROVED, REJECTED, CANCELLED; len 12; default=ApprovalRequest.Status.DRAFT |
| `current_level` | PositiveSmallIntegerField |  |  | default=1 |
| `total_levels` | PositiveSmallIntegerField |  |  | default=0 |
| `round_number` | PositiveSmallIntegerField |  |  | default=1 |
| `submitted_by` | ForeignKey | Y | Y | → `users.User` PROTECT |
| `submitted_at` | DateTimeField | Y |  |  |
| `level_entered_at` | DateTimeField | Y |  |  |
| `decided_at` | DateTimeField | Y |  |  |
| `document` | GenericForeignKey |  |  |  |

- **constraint** `approval_request_one_open_per_document`: UniqueConstraint
- **index** `idx_apreq_queue`: ['status', 'current_level']
- **index** `idx_apreq_target`: ['content_type', 'object_id']
- **index** `idx_apreq_list`: ['company', 'status', '-created_at']

### `ApprovalWorkflow` → `approval_workflow`  ordering=['document_type', 'company']

> A named ladder of levels for one document type (optionally per company).


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `code` | CharField |  |  | unique; len 50 |
| `name` | CharField |  |  | len 100 |
| `document_type` | CharField |  | Y | choices: PAYMENT, DEPOSIT, ORDER; len 30 |
| `company` | CharField |  |  | choices: OIL, BEVERAGES, MART; len 20; default='' |
| `restart_on_reject` | BooleanField |  |  | default=True |
| `forbid_self_approval` | BooleanField |  |  | default=True |
| `is_active` | BooleanField |  |  | default=True |

- **constraint** `approval_workflow_one_active_per_type_company`: UniqueConstraint


## `attachments` — 1 models

### `Attachment` → `payment_attachment`  ordering=['-created_at']

> One uploaded file, attached generically to a payment or deposit.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `content_type` | ForeignKey |  | Y | → `contenttypes.ContentType` CASCADE |
| `object_id` | PositiveBigIntegerField |  |  |  |
| `attachment_type` | CharField |  | Y | choices: CHEQUE_IMAGE, UPI_SCREENSHOT, DEPOSIT_SLIP, DEPOSIT_RECEIPT; len 20 |
| `stored_name` | CharField |  |  | unique; len 120 |
| `original_name` | CharField |  |  | len 255 |
| `uploaded_by` | ForeignKey |  | Y | → `users.User` PROTECT |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |
| `document` | GenericForeignKey |  |  |  |

- **index** `idx_att_target`: ['content_type', 'object_id']


## `audit` — 1 models

### `AuditLog` → `audit_log`  ordering=['-created_at']

> A simple record of a change made on an admin page.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `user` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `username` | CharField |  |  | len 150; default='' |
| `page` | CharField |  |  | len 100; default='' |
| `action` | CharField |  |  | len 20; default='' |
| `record` | CharField |  |  | len 255; default='' |
| `field` | CharField |  |  | len 100; default='' |
| `old_value` | TextField |  |  | default='' |
| `new_value` | TextField |  |  | default='' |
| `created_at` | DateTimeField |  | Y |  |


## `core` — 1 models

### `DocumentCounter` → `core_document_counter`

> Gapless per-scope sequence for receipt / deposit numbers.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `doc_type` | CharField |  |  | len 20 |
| `company` | CharField |  |  | len 20 |
| `fiscal_year` | CharField |  |  | len 20 |
| `prefix` | CharField |  |  | len 20 |
| `last_number` | PositiveIntegerField |  |  | default=0 |
| `updated_at` | DateTimeField |  |  |  |

- **constraint** `core_doc_counter_scope_uq`: UniqueConstraint


## `devices` — 2 models

### `UserDevice` → `devices_user_device`  ordering=['-last_active']

> One installed app on one device, for one user.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `device_id` | CharField |  |  | len 64 |
| `platform` | CharField |  |  | choices: ANDROID, IOS, WEB, DESKTOP; len 20 |
| `app_type` | CharField |  |  | choices: MOBILE, TABLET, WEB, ADMIN_WEB, PARTNER_WEB, DESKTOP; len 20 |
| `app_version` | CharField |  |  | len 20 |
| `build_number` | PositiveIntegerField |  |  |  |
| `device_name` | CharField |  |  | len 100; default='' |
| `manufacturer` | CharField |  |  | len 50; default='' |
| `device_model` | CharField |  |  | len 100; default='' |
| `os_name` | CharField |  |  | len 20; default='' |
| `os_version` | CharField |  |  | len 20; default='' |
| `browser_name` | CharField |  |  | len 30; default='' |
| `browser_version` | CharField |  |  | len 30; default='' |
| `language` | CharField |  |  | len 10; default='' |
| `timezone` | CharField |  |  | len 64; default='' |
| `first_login` | DateTimeField |  |  |  |
| `last_login` | DateTimeField |  |  |  |
| `last_active` | DateTimeField |  |  |  |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **constraint** `device_user_deviceid_uq`: UniqueConstraint
- **index** `device_user_active_idx`: ['user', 'is_active']
- **index** `device_plat_build_idx`: ['platform', 'app_type', 'build_number']
- **index** `device_last_active_idx`: ['last_active']

### `VersionPolicy` → `devices_version_policy`  ordering=['platform']

> The minimum acceptable app version for one mobile platform.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `platform` | CharField |  |  | choices: ANDROID, IOS; len 20 |
| `required_version` | CharField |  |  | len 20 |
| `required_build` | PositiveIntegerField |  |  |  |
| `store_url` | CharField |  |  | len 200; default='' |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **constraint** `versionpolicy_one_active_per_platform_uq`: UniqueConstraint


## `einvoice` — 3 models

### `EwayBill` → `einvoice_ewaybill`  ordering=['-created_at']

> An e-Way Bill (standalone or generated by IRN).


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `environment` | CharField |  |  | choices: sandbox, production; len 10; default='sandbox' |
| `irn_record` | ForeignKey | Y | Y | → `einvoice.IrnRecord` SET_NULL |
| `order_id` | BigIntegerField | Y | Y |  |
| `ewb_no` | CharField | Y |  | unique; len 15 |
| `ewb_date` | DateTimeField | Y |  |  |
| `valid_till` | DateTimeField | Y |  |  |
| `ewb_status` | CharField | Y |  | len 3 |
| `supplier_gstin` | CharField | Y |  | len 15 |
| `buyer_gstin` | CharField | Y |  | len 15 |
| `doc_type` | CharField | Y |  | len 15 |
| `doc_no` | CharField | Y |  | len 16 |
| `doc_date` | DateField | Y |  |  |
| `trans_mode` | CharField | Y |  | len 1 |
| `trans_distance` | IntegerField | Y |  |  |
| `transporter_id` | CharField | Y |  | len 15 |
| `transporter_name` | CharField | Y |  | len 100 |
| `trans_doc_no` | CharField | Y |  | len 15 |
| `trans_doc_date` | DateField | Y |  |  |
| `vehicle_no` | CharField | Y |  | len 15 |
| `vehicle_type` | CharField | Y |  | len 1 |
| `part_b_updated` | BooleanField |  |  | default=False |
| `generation_status` | CharField |  | Y | choices: PENDING, GENERATED, FAILED, CANCELLED; len 20; default='PENDING' |
| `cancelled_at` | DateTimeField | Y |  |  |
| `cancel_reason_code` | CharField | Y |  | len 2 |
| `cancel_remarks` | CharField | Y |  | len 100 |
| `request_payload` | JSONField | Y |  |  |
| `response_payload` | JSONField | Y |  |  |
| `error_details` | JSONField | Y |  |  |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |

### `IrnGenerationLog` → `einvoice_irn_generation_log`  ordering=['-created_at']

> Audit log of every automatic (and manual) IRN generation attempt for a SAP


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `docentry` | IntegerField |  | Y |  |
| `company_db` | CharField | Y |  | len 100 |
| `environment` | CharField |  |  | choices: sandbox, production; len 10; default='sandbox' |
| `trigger` | CharField |  | Y | choices: invoice_create, poll, manual, retry; len 20; default='manual' |
| `attempt_no` | PositiveIntegerField |  |  | default=1 |
| `outcome` | CharField |  | Y | choices: SUCCESS, FAILED, SKIPPED; len 10 |
| `doc_no` | CharField | Y |  | len 16 |
| `irn` | CharField | Y |  | len 64 |
| `ack_no` | CharField | Y |  | len 20 |
| `irn_record` | ForeignKey | Y | Y | → `einvoice.IrnRecord` SET_NULL |
| `error_code` | CharField | Y |  | len 20 |
| `error_message` | TextField | Y |  |  |
| `validation_errors` | JSONField | Y |  |  |
| `duration_ms` | IntegerField | Y |  |  |
| `created_at` | DateTimeField |  | Y |  |

- **index** `einvoice_ir_docentr_ef95d6_idx`: ['docentry', '-created_at']
- **index** `einvoice_ir_outcome_67485b_idx`: ['outcome', '-created_at']

### `IrnRecord` → `einvoice_irn`  ordering=['-created_at']

> One e-Invoice document reported to the IRP → one IRN.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `environment` | CharField |  |  | choices: sandbox, production; len 10; default='sandbox' |
| `order_id` | BigIntegerField | Y | Y |  |
| `source` | CharField | Y |  | len 50 |
| `supplier_gstin` | CharField |  |  | len 15 |
| `doc_type` | CharField |  |  | choices: INV, CRN, DBN; len 3 |
| `doc_no` | CharField |  | Y | len 16 |
| `doc_date` | DateField |  |  |  |
| `financial_year` | CharField |  |  | len 7 |
| `buyer_gstin` | CharField | Y |  | len 15 |
| `generation_status` | CharField |  | Y | choices: PENDING, GENERATED, FAILED, CANCELLED; len 20; default='PENDING' |
| `irn` | CharField | Y |  | unique; len 64 |
| `ack_no` | CharField | Y |  | len 20 |
| `ack_date` | DateTimeField | Y |  |  |
| `signed_invoice` | TextField | Y |  |  |
| `signed_qr_code` | TextField | Y |  |  |
| `irp_status` | CharField | Y |  | len 3 |
| `ewb_no` | CharField | Y |  | len 15 |
| `ewb_date` | DateTimeField | Y |  |  |
| `cancelled_at` | DateTimeField | Y |  |  |
| `cancel_reason_code` | CharField | Y |  | len 1 |
| `cancel_remarks` | CharField | Y |  | len 100 |
| `request_payload` | JSONField | Y |  |  |
| `response_payload` | JSONField | Y |  |  |
| `error_details` | JSONField | Y |  |  |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |

- **constraint** `einvoice_irn_doc_uq`: UniqueConstraint


## `invoice` — 4 models

### `CreditLimitLogs` → `credit_limit_logs`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `invoice_log` | ForeignKey |  | Y | → `invoice.InvoiceLog` CASCADE; unique |
| `jsap_doc_id` | IntegerField |  |  |  |
| `party_name` | CharField |  |  | len 255 |
| `created_at` | DateTimeField |  |  |  |
| `created_by` | ForeignKey |  | Y | → `users.User` CASCADE |

### `InvocieHistory` → `invoice_history`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `invoice_log` | ForeignKey | Y | Y | → `invoice.InvoiceLog` CASCADE |
| `so_number` | CharField |  |  | len 100 |
| `party_name` | CharField |  |  | len 255 |
| `total_amount` | DecimalField |  |  |  |
| `status` | CharField |  |  | len 20 |
| `rejection_reason` | TextField | Y |  |  |
| `error_message` | TextField | Y |  |  |
| `invoice_payload` | JSONField |  |  |  |
| `created_at` | DateTimeField |  |  |  |
| `created_by` | CharField | Y |  | len 125 |
| `device_id` | CharField |  |  | len 64; default='' |
| `device_name` | CharField |  |  | len 150; default='' |

### `InvoiceLog` → `invoice_log`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `so_number` | CharField |  |  | len 100 |
| `party_name` | CharField |  |  | len 255 |
| `total_amount` | DecimalField |  |  |  |
| `branch` | CharField | Y |  | len 25 |
| `warehouse` | CharField | Y |  | len 25 |
| `status` | CharField |  |  | choices: PENDING, APPROVED, REJECTED, EDITED, ERROR, POSTED_TO_SAP; len 20; default='PENDING' |
| `rejection_reason` | TextField | Y |  |  |
| `error_message` | TextField | Y |  |  |
| `invoice_payload` | JSONField |  |  |  |
| `sap_doc_num` | CharField | Y |  | len 50 |
| `sap_doc_entry` | CharField | Y |  | len 50 |
| `supersedes` | ForeignKey | Y | Y | → `invoice.InvoiceLog` SET_NULL |
| `created_at` | DateTimeField |  |  |  |
| `created_by` | ForeignKey |  | Y | → `users.User` CASCADE |
| `is_deleted` | BooleanField |  |  | default=False |
| `deleted_at` | DateTimeField | Y |  |  |
| `deleted_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `delete_reason` | TextField | Y |  |  |

### `InvoiceRefLogs` → `invoice_ref_logs`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `ref_id` | CharField |  |  | len 25 |
| `card_name` | CharField |  |  | len 255 |
| `doc_date` | DateField |  |  |  |
| `so_number` | CharField |  |  | len 255 |
| `status` | CharField |  |  | len 255 |
| `error_message` | TextField | Y |  |  |
| `posted_by` | ForeignKey |  | Y | → `users.User` CASCADE |
| `posted_at` | DateTimeField |  |  |  |


## `legal` — 4 models

### `LabelData` → `labels`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `label_file` | FileField |  |  | len 100 |
| `parameter_json` | JSONField | Y |  |  |
| `uploaded_at` | DateTimeField |  |  |  |

### `LabelItem` → `label_item`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `item_name` | CharField |  |  | len 255 |
| `created_at` | DateTimeField |  |  |  |

### `LabelNutrition` → `label_nutrition`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `label_item` | ForeignKey |  | Y | → `legal.LabelItem` CASCADE |
| `uom` | ForeignKey | Y | Y | → `legal.NutritionUOM` SET_NULL |
| `nutrition_name` | CharField |  |  | len 125 |
| `per_serving` | DecimalField |  |  |  |
| `per_100gm` | DecimalField |  |  |  |

### `NutritionUOM` → `nutrition_uom`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `uom_name` | CharField |  |  | len 25 |
| `uom_unit` | CharField |  |  | len 5 |


## `notifications` — 1 models

### `Notification` → `public"."notifications_notification`  ordering=['-created_at']

> One notification for one recipient, owned by the framework.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `company` | ForeignKey | Y | Y | → `users.Company` CASCADE |
| `event_type` | CharField |  | Y | len 50 |
| `title` | CharField |  |  | len 255; default='' |
| `message` | TextField |  |  |  |
| `content_type` | ForeignKey | Y | Y | → `contenttypes.ContentType` PROTECT |
| `object_id` | PositiveBigIntegerField | Y |  |  |
| `is_read` | BooleanField |  |  | default=False |
| `created_at` | DateTimeField |  |  |  |
| `entity` | GenericForeignKey |  |  |  |

- **index** `nfw_user_created_idx`: ['user', '-created_at']
- **index** `nfw_user_read_idx`: ['user', 'is_read']
- **index** `nfw_company_created_idx`: ['company', '-created_at']
- **index** `nfw_entity_idx`: ['content_type', 'object_id']


## `orders` — 25 models

### `Branches` → `branches`  **unmanaged**

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `bpl_id` | CharField | Y |  | len 50 |
| `bpl_name` | CharField | Y |  | len 100 |
| `category` | CharField | Y |  | len 50 |

### `Categories` → `categories`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `category` | CharField |  |  | len 255 |

### `DispatchLocation` → `dispatch_locations`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `name` | CharField |  |  | len 100 |
| `code` | CharField |  |  | len 50 |
| `address` | TextField | Y |  |  |
| `city` | CharField | Y |  | len 100 |
| `state` | CharField | Y |  | len 100 |
| `pincode` | CharField | Y |  | len 20 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField | Y |  |  |

### `Notification` → `notifications`  ordering=['-created_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `order` | ForeignKey |  | Y | → `orders.Order` CASCADE |
| `message` | TextField |  |  |  |
| `is_read` | BooleanField |  |  | default=False |
| `created_at` | DateTimeField |  |  |  |

- **index** `notif_user_created_idx`: ['user', '-created_at']
- **index** `notif_user_isread_idx`: ['user', 'is_read']

### `Order` → `orders`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `order_number` | CharField |  |  | unique; len 50 |
| `card_code` | CharField |  |  | len 50 |
| `card_name` | CharField |  |  | len 255 |
| `bill_to_id` | IntegerField |  |  | default=0 |
| `bill_to_address` | TextField | Y |  |  |
| `ship_to_id` | IntegerField |  |  | default=0 |
| `ship_to_address` | TextField | Y |  |  |
| `dispatch_from_id` | IntegerField |  |  | default=0 |
| `dispatch_from_name` | CharField | Y |  | len 100 |
| `company` | CharField | Y |  | len 100 |
| `po_number` | CharField | Y |  | len 100 |
| `warehouse_code` | CharField |  |  | len 20; default='' |
| `is_foc` | BooleanField |  |  | default=False |
| `remarks` | TextField | Y |  |  |
| `total_amount` | DecimalField |  |  | default=0 |
| `status` | ForeignKey |  | Y | → `orders.OrderStatus` PROTECT |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `created_at` | DateTimeField |  |  |  |
| `delivery_date` | DateField | Y |  |  |
| `order_type` | CharField |  |  | choices: PARTY, STAFF, DISTRIBUTOR; len 20; default='PARTY' |
| `employee_id` | CharField | Y |  | len 255 |
| `sap_created` | BooleanField |  |  | default=False |
| `sap_doc_number` | CharField | Y |  | len 100 |
| `quotation_cancelled` | BooleanField |  |  | default=False |
| `quotation_cancelled_at` | DateTimeField | Y |  |  |
| `quotation_cancelled_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `approved_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `approved_at` | DateTimeField | Y |  |  |
| `rejected_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `rejected_at` | DateTimeField | Y |  |  |
| `rejection_reason` | TextField | Y |  |  |
| `reject_reason` | TextField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

### `OrderFlowConfig` → `order_flow_config`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `flow_type` | CharField |  |  | unique; len 20; default='ASM' |
| `rate_approval_enabled` | BooleanField |  |  | default=True |
| `billing_enabled` | BooleanField |  |  | default=True |
| `auditor_enabled` | BooleanField |  |  | default=True |
| `rate_conditions` | JSONField |  |  | default=<class 'list'> |
| `updated_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

### `OrderItem` → `order_items`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `order` | ForeignKey |  | Y | → `orders.Order` CASCADE |
| `item_code` | CharField |  |  | len 50 |
| `item_name` | CharField | Y |  | len 255 |
| `category` | CharField | Y |  | len 100 |
| `brand` | CharField | Y |  | len 100 |
| `sub_group` | CharField | Y |  | len 100 |
| `item_type` | CharField | Y |  | len 100 |
| `qty` | DecimalField |  |  | default=0 |
| `pcs` | DecimalField |  |  | default=0 |
| `boxes` | DecimalField |  |  | default=0 |
| `ltrs` | DecimalField |  |  | default=0 |
| `price_list_basic` | DecimalField |  |  | default=0 |
| `basic_price` | DecimalField |  |  | default=0 |
| `total` | DecimalField |  |  | default=0 |
| `tax_rate` | DecimalField |  |  | default=0 |
| `scheme` | ForeignKey | Y | Y | → `users.SchemeProduct` SET_NULL |
| `qty_scheme` | DecimalField | Y |  | default=0 |
| `is_scheme_visible` | BooleanField |  |  | default=False |
| `is_auto_free` | BooleanField |  |  | default=False |
| `combo_source_code` | CharField | Y |  | len 50 |

### `OrderItemApprovalMapping` → `order_item_approval_mapping`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `order` | ForeignKey |  | Y | → `orders.Order` CASCADE |
| `order_item` | ForeignKey |  | Y | → `orders.OrderItem` CASCADE |
| `approver` | ForeignKey |  | Y | → `users.User` CASCADE |

- **unique_together**: [['order_item', 'approver']]

### `OrderItemScheme` → `order_item_schemes`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `order_item` | ForeignKey |  | Y | → `orders.OrderItem` CASCADE |
| `scheme` | ForeignKey | Y | Y | → `users.SchemeProduct` SET_NULL |
| `qty_scheme` | DecimalField | Y |  | default=0 |
| `scheme_v2` | ForeignKey | Y | Y | → `orders.Scheme` PROTECT |
| `benefit` | ForeignKey | Y | Y | → `orders.SchemeBenefit` SET_NULL |
| `benefit_item_code` | CharField | Y |  | len 50 |
| `benefit_uom` | CharField |  |  | len 10; default='' |
| `benefit_qty` | DecimalField |  |  | default=0 |
| `computed_qty` | DecimalField |  |  | default=0 |
| `is_manual_override` | BooleanField |  |  | default=False |
| `scope_type` | CharField |  |  | len 20; default='' |
| `scope_value` | CharField |  |  | len 100; default='' |

### `OrderRateApproval` → `order_rate_approvals`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `order` | ForeignKey |  | Y | → `orders.Order` CASCADE |
| `approver` | ForeignKey |  | Y | → `users.User` CASCADE |
| `status` | CharField |  |  | choices: PENDING, APPROVED, REJECTED; len 20; default='PENDING' |
| `remarks` | TextField | Y |  |  |
| `approved_at` | DateTimeField | Y |  |  |
| `created_at` | DateTimeField |  |  |  |

- **unique_together**: [['order', 'approver']]

### `OrderStatus` → `order_statuses`

> Represents the status of an order.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `code` | CharField | Y |  | unique; len 50 |
| `name` | CharField |  |  | len 100 |
| `created_at` | DateTimeField |  |  | default=<function now at 0x000001C185545 |

### `OrdersLog` → `orders_log`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `order` | ForeignKey |  | Y | → `orders.Order` CASCADE |
| `action` | ForeignKey | Y | Y | → `orders.OrderStatus` SET_NULL |
| `performed_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `remarks` | TextField | Y |  |  |
| `created_at` | DateTimeField |  |  |  |

### `Parties` → `parties`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `card_code` | CharField |  |  | unique; len 50 |
| `card_name` | CharField |  |  | len 255 |
| `address` | TextField | Y |  |  |
| `state` | CharField | Y |  | len 50 |
| `main_group` | CharField | Y |  | len 50 |

### `PartyAddress` → `party_addresses`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `card_code` | CharField |  |  | len 50 |
| `full_address` | TextField | Y |  |  |
| `gst_number` | CharField | Y |  | len 50 |
| `address_type` | CharField | Y |  | len 10 |
| `address_name` | CharField | Y |  | len 100 |
| `category` | CharField | Y |  | len 50 |

### `PartyOrderFlowConfig` → `party_order_flow_config`

> Per-party, per-category, per-role order flow override. When a party has a


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `card_code` | CharField |  | Y | len 50 |
| `category` | CharField |  |  | len 50; default='' |
| `flow_type` | CharField |  |  | len 20; default='ASM' |
| `rate_approval_enabled` | BooleanField |  |  | default=True |
| `billing_enabled` | BooleanField |  |  | default=True |
| `auditor_enabled` | BooleanField |  |  | default=True |
| `rate_conditions` | JSONField |  |  | default=<class 'list'> |
| `updated_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **unique_together**: [['card_code', 'category', 'flow_type']]

### `ProductDetails` → `product_details`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `item_code` | CharField |  |  | unique; len 50 |
| `item_name` | CharField |  |  | len 255 |
| `category` | CharField | Y |  | len 100 |
| `brand` | CharField | Y |  | len 100 |
| `variety` | CharField | Y |  | len 100 |
| `sal_factor2` | DecimalField |  |  | default=1 |
| `tax_rate` | DecimalField |  |  | default=0 |
| `sal_pack_unit` | CharField | Y |  | len 50 |

### `PushToken` → `push_tokens`  ordering=['-updated_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `token` | CharField |  |  | unique; len 255 |
| `platform` | CharField |  |  | len 20; default='' |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **index** `pushtoken_user_active_idx`: ['user', 'is_active']

### `RateApproverRule` → `rate_approver_rules`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `approver` | ForeignKey |  | Y | → `users.User` CASCADE |
| `category` | CharField |  |  | len 100 |
| `variety` | CharField | Y |  | len 100 |
| `sub_group` | CharField | Y |  | len 100 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |

- **unique_together**: [['category', 'sub_group']]

### `Scheme` → `schemes`  ordering=['-priority', 'name']

> One offer. Carries no product, no geography and no vendor — those live in


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `code` | CharField |  |  | unique; len 50 |
| `name` | CharField |  |  | len 255 |
| `description` | TextField |  |  | default='' |
| `category` | CharField |  | Y | choices: OIL, BEVERAGES, MART; len 20; default='' |
| `valid_from` | DateField | Y |  |  |
| `valid_to` | DateField | Y |  |  |
| `is_active` | BooleanField |  |  | default=True |
| `priority` | IntegerField |  |  | default=0 |
| `stackable` | BooleanField |  |  | default=False |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **index** `scheme_active_window_idx`: ['is_active', 'valid_from', 'valid_to']

### `SchemeAssignment` → `scheme_assignments`  ordering=['scheme_id', 'scope_type', 'scope_value']

> Who a scheme reaches.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `scheme` | ForeignKey |  | Y | → `orders.Scheme` CASCADE |
| `scope_type` | CharField |  |  | choices: PARTY, MAIN_GROUP, STATE, CATEGORY, ALL; len 20; default='PARTY' |
| `scope_value` | CharField |  |  | len 100; default='' |
| `category` | CharField |  |  | len 20; default='' |
| `is_exclusion` | BooleanField |  |  | default=False |
| `valid_from` | DateField | Y |  |  |
| `valid_to` | DateField | Y |  |  |
| `is_active` | BooleanField |  |  | default=True |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `created_at` | DateTimeField |  |  |  |

- **unique_together**: [['scheme', 'scope_type', 'scope_value', 'category']]
- **index** `scheme_assign_scope_idx`: ['scope_type', 'scope_value', 'is_active']

### `SchemeBenefit` → `scheme_benefits`  ordering=['id']

> What a scheme gives away. Several rows = several giveaway items, replacing


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `scheme` | ForeignKey |  | Y | → `orders.Scheme` CASCADE |
| `free_item_code` | CharField | Y | Y | len 50 |
| `free_uom` | CharField |  |  | choices: PCS, BOX; len 10; default='PCS' |
| `per_qty` | DecimalField |  |  | default=0 |
| `free_qty` | DecimalField |  |  | default=0 |
| `max_free_qty` | DecimalField | Y |  |  |

### `SchemeTrigger` → `scheme_triggers`  ordering=['id']

> What earns a scheme.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `scheme` | ForeignKey |  | Y | → `orders.Scheme` CASCADE |
| `match_type` | CharField |  |  | choices: ITEM, SUB_GROUP, VARIETY, BRAND, CATEGORY, ALL; len 20; default='ITEM' |
| `match_value` | CharField |  |  | len 100; default='' |
| `min_qty` | DecimalField |  |  | default=0 |
| `min_uom` | CharField |  |  | choices: PCS, BOX; len 10; default='PCS' |
| `applies_to` | CharField |  |  | choices: PAID_LINE, FREE_LINE, BOTH; len 20; default='PAID_LINE' |

- **index** `scheme_trigger_match_idx`: ['match_type', 'match_value']

### `StaffProductPrice` → `staff_product_prices`  ordering=['-created_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `product` | ForeignKey |  | Y | → `sap_sync.Product` CASCADE |
| `rate` | DecimalField |  |  |  |
| `created_at` | DateTimeField |  |  |  |

### `Template` → `order_template`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `temp_id` | AutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `order` | ForeignKey |  | Y | → `orders.Order` CASCADE |
| `sub_group` | CharField | Y |  | len 255 |
| `created_at` | DateTimeField |  |  |  |

- **unique_together**: [['user', 'order']]

### `WebPushSubscription` → `web_push_subscriptions`  ordering=['-updated_at']

> A browser Web Push subscription (Phase 3).


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `endpoint` | TextField |  |  | unique |
| `p256dh` | CharField |  |  | len 255 |
| `auth` | CharField |  |  | len 255 |
| `user_agent` | CharField |  |  | len 255; default='' |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **index** `webpush_user_active_idx`: ['user', 'is_active']


## `payments` — 11 models

### `BankDeposit` → `payment_bank_deposit`  ordering=['-deposit_date', '-id']

> Banking one or more receipts (SAP: Deposits / ODPS).


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `deposit_no` | CharField |  |  | unique; len 40 |
| `company` | CharField |  | Y | choices: OIL, BEVERAGES, MART; len 20 |
| `company_db` | CharField |  |  | len 100; default='' |
| `deposit_date` | DateField |  | Y |  |
| `deposited_by` | ForeignKey | Y | Y | → `payments.CollectionPerson` PROTECT |
| `bank_key` | CharField |  |  | len 80; default='' |
| `bank_code` | CharField |  |  | len 30; default='' |
| `bank_gl_account` | CharField |  |  | len 50; default='' |
| `bank_display_name` | CharField |  |  | len 150; default='' |
| `deposit_type` | CharField |  |  | choices: CASH, CHEQUE, MIXED; len 10; default=BankDeposit.DepositType.CASH |
| `collected_amount` | DecimalField |  |  | default=0 |
| `deposit_amount` | DecimalField |  |  |  |
| `shortfall_reason` | TextField |  |  | default='' |
| `bank_charge` | DecimalField |  |  | default=0 |
| `currency` | CharField |  |  | len 3; default='INR' |
| `slip_number` | CharField |  |  | len 100; default='' |
| `remarks` | TextField |  |  | default='' |
| `status` | CharField |  | Y | choices: DRAFT, PENDING_APPROVAL, APPROVED, REJECTED, POSTING_TO_SAP, POSTED; len 20; default=BankDeposit.Status.DRAFT |
| `sap_doc_entry` | IntegerField | Y | Y |  |
| `sap_doc_num` | IntegerField | Y |  |  |
| `sap_trans_id` | IntegerField | Y |  |  |
| `sap_posted_at` | DateTimeField | Y |  |  |
| `sap_response` | TextField |  |  | default='' |
| `sap_raw_error` | TextField |  |  | default='' |
| `sap_raw_error_code` | CharField |  |  | len 20; default='' |
| `sap_cancelled_at` | DateTimeField | Y |  |  |
| `sap_cancellation_response` | TextField |  |  | default='' |
| `sap_reconciled_at` | DateTimeField | Y |  |  |
| `attachments` | ManyToManyField | Y |  | → `attachments.Attachment` DO_NOTHING |
| `approvals` | ManyToManyField | Y |  | → `approvals.ApprovalRequest` DO_NOTHING |

- **constraint** `bank_deposit_amount_positive`: CheckConstraint
- **constraint** `bank_deposit_not_over_collected`: CheckConstraint
- **constraint** `bank_deposit_shortfall_requires_reason`: CheckConstraint
- **index** `idx_dep_company_status`: ['company', 'status', '-deposit_date']
- **index** `idx_dep_bank_date`: ['bank_gl_account', '-deposit_date']

### `BankDepositLine` → `payment_bank_deposit_line`

> One receipt inside a deposit.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `deposit` | ForeignKey |  | Y | → `payments.BankDeposit` CASCADE |
| `receipt` | ForeignKey |  | Y | → `payments.PaymentReceipt` PROTECT |
| `amount` | DecimalField |  |  |  |
| `created_at` | DateTimeField |  |  |  |

- **constraint** `bank_deposit_receipt_once_uq`: UniqueConstraint
- **index** `idx_depline_deposit`: ['deposit']

### `CashDenomination` → `payment_cash_denomination`  ordering=['-denomination']

> Note breakdown for a cash tender line.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `entry` | ForeignKey |  | Y | → `payments.PaymentMethodEntry` CASCADE |
| `denomination` | PositiveSmallIntegerField |  |  | choices: 10, 20, 50, 100, 200, 500 |
| `quantity` | PositiveIntegerField |  |  |  |

- **constraint** `payment_denomination_uq`: UniqueConstraint
- **constraint** `payment_denomination_qty_positive`: CheckConstraint

### `CollectionPerson` → `payment_collection_person`  ordering=['name']

> The manually-maintained "Received From" list.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |
| `name` | CharField |  |  | len 120 |
| `code` | CharField |  |  | unique; len 30 |
| `company` | CharField |  |  | choices: OIL, BEVERAGES, MART; len 20; default='' |
| `phone` | CharField |  |  | len 20; default='' |
| `is_active` | BooleanField |  |  | default=True |

- **index** `idx_collperson_company`: ['company', 'is_active']

### `PaymentAllocation` → `payment_invoice_allocation`  ordering=['invoice_due_date', 'sap_doc_num']

> Applies part of a receipt to one open SAP invoice.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `receipt` | ForeignKey |  | Y | → `payments.PaymentReceipt` CASCADE |
| `sap_doc_entry` | IntegerField |  |  |  |
| `sap_doc_num` | IntegerField | Y |  |  |
| `invoice_type` | IntegerField |  |  | default=13 |
| `invoice_date` | DateField | Y |  |  |
| `invoice_due_date` | DateField | Y |  |  |
| `invoice_total` | DecimalField |  |  | default=0 |
| `balance_at_selection` | DecimalField |  |  | default=0 |
| `amount_applied` | DecimalField |  |  |  |
| `created_at` | DateTimeField |  |  |  |

- **constraint** `payment_allocation_uq`: UniqueConstraint
- **constraint** `payment_allocation_amount_positive`: CheckConstraint
- **index** `idx_alloc_docentry`: ['sap_doc_entry']

### `PaymentMethodEntry` → `payment_method_entry`  ordering=['id']

> One tender line on a receipt: cash, UPI or cheque.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `receipt` | ForeignKey |  | Y | → `payments.PaymentReceipt` CASCADE |
| `method` | CharField |  |  | choices: CASH, UPI, CHEQUE; len 20 |
| `amount` | DecimalField |  |  |  |
| `upi_reference` | CharField |  |  | len 60; default='' |
| `cheque_number` | CharField |  |  | len 30; default='' |
| `bank_name` | CharField |  |  | len 120; default='' |
| `cheque_date` | DateField | Y |  |  |
| `sap_check_key` | IntegerField | Y |  |  |

- **constraint** `payment_method_amount_positive`: CheckConstraint
- **constraint** `payment_method_cheque_requires_details`: CheckConstraint

### `PaymentMethodMapping` → `payment_method_mapping`  ordering=['company', 'payment_method', 'priority']

> Which SAP House Bank Account each payment method posts to.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `company` | CharField |  | Y | choices: OIL, BEVERAGES, MART; len 20 |
| `payment_method` | CharField |  |  | len 20 |
| `bank_key` | CharField |  |  | len 80 |
| `priority` | PositiveSmallIntegerField |  |  | default=0 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **constraint** `payment_method_mapping_one_active`: UniqueConstraint

### `PaymentReceipt` → `payment_receipt`  ordering=['-payment_date', '-id']

> A payment received from a party (SAP: IncomingPayments / ORCT).


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `created_at` | DateTimeField |  | Y |  |
| `updated_at` | DateTimeField |  |  |  |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `receipt_no` | CharField |  |  | unique; len 40 |
| `company` | CharField |  | Y | choices: OIL, BEVERAGES, MART; len 20 |
| `company_db` | CharField |  |  | len 100; default='' |
| `card_code` | CharField |  | Y | len 50 |
| `card_name` | CharField |  |  | len 200; default='' |
| `received_from_type` | CharField |  |  | choices: PARTY, PERSON; len 10; default=PaymentReceipt.ReceivedFromType. |
| `received_from_person` | ForeignKey | Y | Y | → `payments.CollectionPerson` PROTECT |
| `payment_date` | DateField |  | Y |  |
| `is_advance` | BooleanField |  |  | default=False |
| `total_amount` | DecimalField |  |  |  |
| `allocated_amount` | DecimalField |  |  | default=0 |
| `currency` | CharField |  |  | len 3; default='INR' |
| `remarks` | TextField |  |  | default='' |
| `status` | CharField |  | Y | choices: DRAFT, PENDING_APPROVAL, APPROVED, REJECTED, POSTING_TO_SAP, POSTED; len 20; default=PaymentReceipt.Status.DRAFT |
| `sap_doc_entry` | IntegerField | Y | Y |  |
| `sap_doc_num` | IntegerField | Y |  |  |
| `sap_trans_id` | IntegerField | Y |  |  |
| `sap_posted_at` | DateTimeField | Y |  |  |
| `sap_branch_id` | IntegerField | Y |  |  |
| `sap_branch_name` | CharField |  |  | len 100; default='' |
| `sap_response` | TextField |  |  | default='' |
| `sap_raw_error` | TextField |  |  | default='' |
| `sap_raw_error_code` | CharField |  |  | len 20; default='' |
| `sap_cancelled_at` | DateTimeField | Y |  |  |
| `sap_cancellation_response` | TextField |  |  | default='' |
| `sap_reconciled_at` | DateTimeField | Y |  |  |
| `attachments` | ManyToManyField | Y |  | → `attachments.Attachment` DO_NOTHING |
| `approvals` | ManyToManyField | Y |  | → `approvals.ApprovalRequest` DO_NOTHING |

- **constraint** `payment_receipt_amount_positive`: CheckConstraint
- **constraint** `payment_receipt_allocated_within_total`: CheckConstraint
- **constraint** `payment_receipt_person_required`: CheckConstraint
- **index** `idx_rcpt_company_status`: ['company', 'status', '-payment_date']
- **index** `idx_rcpt_party`: ['card_code', 'company']
- **index** `idx_rcpt_sap`: ['status', 'sap_doc_entry']

### `PaymentStatusHistory` → `payment_status_history`  ordering=['-created_at']

> Append-only activity timeline for receipts AND deposits.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `content_type` | ForeignKey |  | Y | → `contenttypes.ContentType` CASCADE |
| `object_id` | PositiveBigIntegerField |  |  |  |
| `action` | CharField |  | Y | choices: CREATED, UPDATED, SUBMITTED, RESUBMITTED, APPROVED, REJECTED; len 20; default=PaymentStatusHistory.Action.STAT |
| `from_status` | CharField |  |  | len 20; default='' |
| `to_status` | CharField |  |  | len 20 |
| `reason` | TextField |  |  | default='' |
| `level` | PositiveSmallIntegerField | Y |  |  |
| `level_label` | CharField |  |  | len 60; default='' |
| `sap_doc_entry` | IntegerField | Y |  |  |
| `sap_doc_num` | IntegerField | Y |  |  |
| `actor_kind` | CharField |  |  | len 20; default='USER' |
| `changed_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `changed_by_username` | CharField |  |  | len 150; default='' |
| `ip_address` | GenericIPAddressField | Y |  | len 39 |
| `created_at` | DateTimeField |  | Y |  |

- **index** `idx_psh_target`: ['content_type', 'object_id', '-created_at']

### `SapCallLog` → `payment_sap_call_log`  ordering=['-created_at']

> One row per HTTP call to the Service Layer.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `content_type` | ForeignKey | Y | Y | → `contenttypes.ContentType` SET_NULL |
| `object_id` | PositiveBigIntegerField | Y |  |  |
| `company_db` | CharField |  |  | len 100; default='' |
| `endpoint` | CharField |  |  | len 300 |
| `request_data` | JSONField | Y |  |  |
| `response_data` | JSONField | Y |  |  |
| `http_status` | IntegerField | Y |  |  |
| `sap_error_code` | CharField |  |  | len 30; default='' |
| `error_message` | TextField |  |  | default='' |
| `sap_doc_entry` | IntegerField | Y |  |  |
| `sap_doc_num` | IntegerField | Y |  |  |
| `status` | CharField |  |  | choices: STARTED, SUCCESS, FAILED; len 10; default=SapCallLog.Status.STARTED |
| `duration_ms` | IntegerField | Y |  |  |
| `created_at` | DateTimeField |  | Y |  |
| `completed_at` | DateTimeField | Y |  |  |

- **index** `idx_scl_status`: ['status', '-created_at']
- **index** `idx_scl_doc`: ['content_type', 'object_id']

### `SapCompanyMap` → `payment_sap_company_map`  ordering=['sort_order', 'company']

> category -> SAP company DB / HANA schema.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `company` | CharField |  |  | choices: OIL, BEVERAGES, MART; unique; len 20 |
| `display_name` | CharField |  |  | len 100 |
| `company_db` | CharField |  |  | len 100 |
| `hana_schema` | CharField |  |  | len 100 |
| `default_bpl_id` | IntegerField | Y |  |  |
| `cash_gl_account` | CharField |  |  | len 50; default='' |
| `deposit_source_gl_account` | CharField |  |  | len 50; default='' |
| `is_active` | BooleanField |  |  | default=True |
| `sort_order` | PositiveSmallIntegerField |  |  | default=0 |


## `sap_sync` — 8 models

### `Branch` → `branches`  **unmanaged** · ordering=['category', 'bpl_id']

> SAP Business Place / Branch from OBPL table


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `bpl_id` | IntegerField |  |  |  |
| `bpl_name` | CharField |  |  | len 200 |
| `category` | CharField |  |  | choices: OIL, BEVERAGES, MART; len 20 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

- **unique_together**: [['bpl_id', 'category']]

### `Party` → `sap_parties`  ordering=['card_code']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `card_code` | CharField |  | Y | len 50 |
| `card_name` | CharField |  |  | len 200 |
| `address` | CharField | Y |  | len 100 |
| `state` | CharField | Y |  | len 100 |
| `main_group` | CharField | Y |  | len 100 |
| `chain` | CharField | Y |  | len 100 |
| `country` | CharField | Y |  | len 50 |
| `card_type` | CharField |  |  | len 1; default='C' |
| `category` | CharField | Y |  | len 20 |
| `synced_at` | DateTimeField |  |  |  |

- **unique_together**: [['card_code', 'category']]

### `PartyAddress` → `sap_party_addresses`  ordering=['card_code', 'address_name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `card_code` | CharField |  | Y | len 50 |
| `address_name` | CharField |  |  | len 100 |
| `address_type` | CharField |  |  | len 1 |
| `gst_number` | CharField | Y |  | len 50 |
| `state` | CharField | Y |  | len 100 |
| `city` | CharField | Y |  | len 100 |
| `zip_code` | CharField | Y |  | len 20 |
| `country` | CharField | Y |  | len 50 |
| `full_address` | TextField | Y |  |  |
| `category` | CharField | Y |  | len 20 |
| `synced_at` | DateTimeField |  |  |  |

- **unique_together**: [['card_code', 'address_name', 'category', 'address_type']]

### `Product` → `sap_products`  ordering=['item_code']

> Product/Item synced from SAP


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `item_code` | CharField |  |  | len 50 |
| `item_name` | CharField | Y |  | len 255 |
| `category` | CharField |  |  | len 20 |
| `sal_factor2` | DecimalField | Y |  |  |
| `tax_rate` | DecimalField | Y |  |  |
| `is_deleted` | CharField | Y |  | len 1; default='N' |
| `variety` | CharField | Y |  | len 100 |
| `type` | CharField | Y |  | len 50 |
| `sub_group` | CharField | Y |  | len 100 |
| `sal_pack_unit` | CharField | Y |  | len 50 |
| `brand` | CharField | Y |  | len 100 |
| `on_hand` | DecimalField | Y |  |  |
| `is_active` | CharField | Y |  | len 50 |
| `synced_at` | DateTimeField |  |  |  |
| `created_at` | DateTimeField |  |  |  |

- **unique_together**: [['item_code', 'category']]

### `SalesOrderLog` → `sales_orders_logs`  ordering=['-created_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `order_id` | CharField | Y |  | len 100 |
| `sap_doc_entry` | IntegerField | Y |  |  |
| `sap_doc_num` | IntegerField | Y |  |  |
| `status` | CharField |  |  | choices: STARTED, SUCCESS, FAILED; len 10; default='STARTED' |
| `request_data` | JSONField | Y |  |  |
| `response_data` | JSONField | Y |  |  |
| `error_message` | TextField | Y |  |  |
| `created_at` | DateTimeField |  |  |  |
| `completed_at` | DateTimeField | Y |  |  |

### `SalesQuotationLog` → `sales_quotation_logs`  ordering=['-created_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `order_id` | CharField | Y |  | len 100 |
| `sap_doc_entry` | IntegerField | Y |  |  |
| `sap_doc_num` | IntegerField | Y |  |  |
| `status` | CharField |  |  | choices: STARTED, SUCCESS, FAILED; len 10; default='STARTED' |
| `request_data` | JSONField | Y |  |  |
| `response_data` | JSONField | Y |  |  |
| `error_message` | TextField | Y |  |  |
| `created_at` | DateTimeField |  |  |  |
| `completed_at` | DateTimeField | Y |  |  |

### `SyncLog` → `sap_sync_logs`  ordering=['-started_at']

> Log of all sync operations


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `sync_type` | CharField |  |  | choices: PRODUCT, PARTY, PARTY_ADDRESS, ALL; len 20 |
| `status` | CharField |  |  | choices: STARTED, SUCCESS, FAILED; len 10; default='STARTED' |
| `records_processed` | IntegerField |  |  | default=0 |
| `records_created` | IntegerField |  |  | default=0 |
| `records_updated` | IntegerField |  |  | default=0 |
| `error_message` | TextField | Y |  |  |
| `started_at` | DateTimeField |  |  |  |
| `completed_at` | DateTimeField | Y |  |  |
| `triggered_by` | CharField |  |  | len 50; default='manual' |

### `SyncSchedule` → `sap_sync_schedules`

> Schedule configuration for automated sync


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | len 100 |
| `sync_type` | CharField |  |  | choices: PRODUCT, PARTY, PARTY_ADDRESS, ALL; len 20; default='ALL' |
| `frequency` | CharField |  |  | choices: HOURLY, DAILY, WEEKLY, CUSTOM; len 20; default='DAILY' |
| `custom_interval_minutes` | IntegerField |  |  | default=60 |
| `hour` | IntegerField |  |  | default=6 |
| `is_active` | BooleanField |  |  | default=False |
| `last_run` | DateTimeField | Y |  |  |
| `next_run` | DateTimeField | Y |  |  |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |


## `tracker` — 15 models

### `AlertNotification` → `tracker_alert_notification`  ordering=['-sent_at']

> One row per (invoice, stage, user) email actually sent by the stuck-alert


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `alert` | ForeignKey | Y | Y | → `tracker.StuckAlert` SET_NULL |
| `invoice` | ForeignKey |  | Y | → `tracker.Invoice` CASCADE |
| `stage` | ForeignKey |  | Y | → `tracker.Stage` CASCADE |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `email` | CharField |  |  | len 254; default='' |
| `days_stuck` | DecimalField |  |  | default=0 |
| `sent_at` | DateTimeField |  |  |  |

- **index** `tracker_ale_invoice_f08fb1_idx`: ['invoice', 'user']
- **index** `tracker_ale_sent_at_4559e6_idx`: ['sent_at']

### `Branch` → `tracker_branch`  ordering=['sort_order', 'name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `is_active` | BooleanField |  |  | default=True |
| `sort_order` | PositiveIntegerField |  |  | default=0 |

### `CashVoucher` → `tracker_cash_voucher`  ordering=['-voucher_date']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `voucher_number` | CharField |  |  | len 100 |
| `voucher_date` | DateField |  |  |  |
| `details` | TextField |  |  | default='' |
| `amount` | DecimalField |  |  | default=0 |
| `unit` | ForeignKey | Y | Y | → `tracker.Unit` PROTECT |
| `status` | CharField |  |  | choices: OPEN, CLOSED; len 10; default=CashVoucher.Status.OPEN |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

### `Category` → `tracker_category`  ordering=['sort_order', 'name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `is_active` | BooleanField |  |  | default=True |
| `sort_order` | PositiveIntegerField |  |  | default=0 |

### `GstRate` → `tracker_gst_rate`  ordering=['sort_order', 'rate']

> GST rate carries a numeric value (used in reports) plus a display label.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `label` | CharField |  |  | unique; len 20 |
| `rate` | DecimalField |  |  | unique |
| `is_active` | BooleanField |  |  | default=True |
| `sort_order` | PositiveIntegerField |  |  | default=0 |

### `GstType` → `tracker_gst_type`  ordering=['sort_order', 'name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `is_active` | BooleanField |  |  | default=True |
| `sort_order` | PositiveIntegerField |  |  | default=0 |

### `Invoice` → `tracker_invoice`  ordering=['-created_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `invoice_date` | DateField |  |  |  |
| `effective_month` | DateField |  |  |  |
| `party_name` | CharField |  |  | len 255 |
| `party_code` | CharField |  |  | len 50; default='' |
| `party_gstin` | CharField |  |  | len 20; default='' |
| `invoice_number` | CharField |  |  | len 100 |
| `taxable_value` | DecimalField |  |  |  |
| `gst_type` | ForeignKey |  | Y | → `tracker.GstType` PROTECT |
| `gst_rate` | ForeignKey |  | Y | → `tracker.GstRate` PROTECT |
| `additional_charge_type` | CharField |  |  | choices: DEMURRAGE, LABOUR_COST, POINT_VALUE; len 20; default='' |
| `additional_charge_amount` | DecimalField |  |  | default=0 |
| `invoice_value` | DecimalField |  |  |  |
| `debit_amount` | DecimalField |  |  | default=0 |
| `hold_amount` | DecimalField |  |  | default=0 |
| `category` | ForeignKey |  | Y | → `tracker.Category` PROTECT |
| `unit` | ForeignKey |  | Y | → `tracker.Unit` PROTECT |
| `branch` | ForeignKey |  | Y | → `tracker.Branch` PROTECT |
| `mode` | ForeignKey |  | Y | → `tracker.InvoiceMode` PROTECT |
| `current_stage` | ForeignKey |  | Y | → `tracker.Stage` PROTECT |
| `status` | CharField |  |  | choices: IN_PROGRESS, COMPLETED; len 20; default=Invoice.Status.IN_PROGRESS |
| `current_stage_entered_at` | DateTimeField |  |  |  |
| `is_locked` | BooleanField |  |  | default=False |
| `rejection_pending` | BooleanField |  |  | default=False |
| `created_by` | ForeignKey |  | Y | → `users.User` PROTECT |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |
| `is_deleted` | BooleanField |  |  | default=False |
| `deleted_at` | DateTimeField | Y |  |  |
| `deleted_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |

- **constraint** `uniq_live_vendor_invoice_number`: UniqueConstraint
- **index** `tracker_inv_current_612b0e_idx`: ['current_stage', 'status']
- **index** `tracker_inv_invoice_3bd1cf_idx`: ['invoice_number']
- **index** `tracker_inv_party_n_fdefdc_idx`: ['party_name']

### `InvoiceMode` → `tracker_mode`  ordering=['sort_order', 'name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `is_active` | BooleanField |  |  | default=True |
| `sort_order` | PositiveIntegerField |  |  | default=0 |

### `PaymentDetail` → `tracker_payment_detail`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `invoice` | OneToOneField |  | Y | → `tracker.Invoice` CASCADE; unique |
| `discount_pct` | DecimalField |  |  | default=0 |
| `tds_pct` | DecimalField |  |  | default=0 |
| `discount_amount` | DecimalField |  |  | default=0 |
| `tds_amount` | DecimalField |  |  | default=0 |
| `hold_added_back` | BooleanField |  |  | default=False |
| `paid_amount` | DecimalField |  |  | default=0 |
| `open_balance` | DecimalField |  |  | default=0 |
| `status` | CharField |  |  | choices: OPEN, PAID; len 10; default=PaymentDetail.Status.OPEN |
| `updated_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `updated_at` | DateTimeField |  |  |  |

### `Stage` → `tracker_stage`  ordering=['order']

> A single desk in the invoice flow.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | len 100 |
| `code` | SlugField |  | Y | unique; len 50 |
| `order` | PositiveIntegerField |  |  | unique |
| `threshold_days` | PositiveIntegerField |  |  | default=3 |
| `status_choices` | JSONField |  |  | default=<class 'list'> |
| `requires_status` | BooleanField |  |  | default=False |
| `can_return` | BooleanField |  |  | default=True |
| `is_terminal` | BooleanField |  |  | default=False |
| `is_active` | BooleanField |  |  | default=True |

### `StageEvent` → `tracker_stage_event`  ordering=['invoice_id', 'entered_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `invoice` | ForeignKey |  | Y | → `tracker.Invoice` CASCADE |
| `stage` | ForeignKey |  | Y | → `tracker.Stage` PROTECT |
| `event_type` | CharField |  |  | choices: RECEIVE, ADVANCE, RETURN; len 10 |
| `stage_status` | CharField |  |  | len 30; default='' |
| `hold_type` | CharField |  |  | choices: FULL, PARTIAL; len 10; default='' |
| `amount` | DecimalField | Y |  |  |
| `receiving_note` | CharField |  |  | choices: ON_TIME, LATE; len 10; default='' |
| `remarks` | TextField |  |  | default='' |
| `acted_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `entered_at` | DateTimeField |  |  |  |
| `exited_at` | DateTimeField | Y |  |  |
| `days_spent` | DecimalField | Y |  |  |
| `created_at` | DateTimeField |  |  |  |

- **index** `tracker_sta_invoice_46fb16_idx`: ['invoice', 'stage']
- **index** `tracker_sta_stage_i_d95d92_idx`: ['stage', 'event_type']

### `StuckAlert` → `tracker_stuck_alert`  ordering=['-days_stuck']

> A raised flag that an invoice has sat at a stage past its threshold.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `invoice` | ForeignKey |  | Y | → `tracker.Invoice` CASCADE |
| `stage` | ForeignKey |  | Y | → `tracker.Stage` CASCADE |
| `stage_entered_at` | DateTimeField |  |  |  |
| `days_stuck` | DecimalField |  |  | default=0 |
| `threshold_days` | PositiveIntegerField |  |  | default=0 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |
| `resolved_at` | DateTimeField | Y |  |  |
| `last_notified_at` | DateTimeField | Y |  |  |

- **unique_together**: [['invoice', 'stage', 'stage_entered_at']]
- **index** `tracker_stu_is_acti_dd41a0_idx`: ['is_active', 'stage']

### `TransporterPayment` → `tracker_transporter_payment`  ordering=['-created_at']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `transport_bill_no` | CharField |  |  | len 100 |
| `party_name` | CharField |  |  | len 255 |
| `discount_pct` | DecimalField |  |  | default=0 |
| `tds_pct` | DecimalField |  |  | default=0 |
| `paid_amount` | DecimalField |  |  | default=0 |
| `open_balance` | DecimalField |  |  | default=0 |
| `status` | CharField |  |  | choices: OPEN, PAID; len 10; default=TransporterPayment.Status.OPEN |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |

### `Unit` → `tracker_unit`  ordering=['sort_order', 'name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `is_active` | BooleanField |  |  | default=True |
| `sort_order` | PositiveIntegerField |  |  | default=0 |

### `UserStageAccess` → `tracker_user_stage_access`  ordering=['user_id', 'stage__order']

> Maps a user to the stage(s) they may see and act on.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `stage` | ForeignKey |  | Y | → `tracker.Stage` CASCADE |
| `is_active` | BooleanField |  |  | default=True |
| `assigned_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `assigned_at` | DateTimeField |  |  |  |

- **unique_together**: [['user', 'stage']]


## `uilabels` — 1 models

### `UILabel` → `uilabels_uilabel`  ordering=['field_key']

> A single dynamic UI field label, editable by admins from the dashboard.


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | BigAutoField |  |  | unique |
| `field_key` | CharField |  |  | unique; len 100 |
| `display_name` | CharField |  |  | len 100 |
| `description` | CharField |  |  | len 255; default='' |
| `is_active` | BooleanField |  |  | default=True |
| `is_enabled` | BooleanField |  |  | default=True |
| `is_required` | BooleanField |  |  | default=False |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |


## `users` — 9 models

### `Company` → `users_company`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 100 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |

### `MainGroup` → `users_maingroup`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 35 |
| `is_active` | BooleanField |  |  | default=True |
| `created_at` | DateTimeField |  |  |  |

### `PartyProductAssignment` → `party_product_assignments`  ordering=['card_code', 'item_code', 'category']

> Maps parties to products with pricing


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `card_code` | CharField |  | Y | len 50 |
| `item_code` | CharField |  | Y | len 50 |
| `category` | CharField |  | Y | choices: OIL, BEVERAGES, MART; len 20 |
| `basic_rate` | DecimalField |  |  | default=0.0 |
| `assigned_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |
| `assigned_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `is_active` | BooleanField |  |  | default=True |
| `is_scheme` | BooleanField |  |  | default=False |
| `parent_item_code` | CharField | Y | Y | len 50 |
| `free_item_code` | CharField | Y | Y | len 50 |
| `free_qty_per_unit` | DecimalField | Y |  |  |
| `scheme` | ForeignKey | Y | Y | → `users.SchemeProduct` SET_NULL |

- **unique_together**: [['card_code', 'item_code', 'category']]

### `SchemeProduct` → `scheme_product`  ordering=['scheme_name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `scheme_id` | AutoField |  |  | unique |
| `state_code` | CharField | Y | Y | len 20 |
| `item_code` | CharField | Y |  | len 100 |
| `scheme_name` | CharField |  |  | len 255 |
| `is_active` | BooleanField |  |  | default=True |

### `State` → `users_state`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 20 |
| `code` | CharField |  |  | unique; len 10 |
| `is_active` | BooleanField |  |  | default=True |

### `User` → `users_user`

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `password` | CharField |  |  | len 128 |
| `username` | CharField |  |  | unique; len 150 |
| `name` | CharField |  |  | len 150 |
| `email` | CharField | Y |  | len 150 |
| `phone` | CharField | Y |  | len 15 |
| `role` | ForeignKey | Y | Y | → `users.UserRole` PROTECT |
| `company` | ForeignKey | Y | Y | → `users.Company` PROTECT |
| `main_group` | ForeignKey | Y | Y | → `users.MainGroup` PROTECT |
| `state` | ForeignKey | Y | Y | → `users.State` PROTECT |
| `category` | ForeignKey | Y | Y | → `orders.Categories` PROTECT |
| `sub_group` | TextField | Y |  |  |
| `extra_pages` | JSONField | Y |  | default=<class 'list'> |
| `is_active` | BooleanField |  |  | default=True |
| `is_staff` | BooleanField |  |  | default=False |
| `is_superuser` | BooleanField |  |  | default=False |
| `date_joined` | DateTimeField |  |  |  |
| `last_login` | DateTimeField | Y |  |  |
| `created_at` | DateTimeField |  |  |  |
| `updated_at` | DateTimeField |  |  |  |
| `created_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `updated_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `groups` | ManyToManyField |  |  | → `auth.Group` |
| `user_permissions` | ManyToManyField |  |  | → `auth.Permission` |
| `extra_roles` | ManyToManyField |  |  | → `users.UserRole` |
| `categories` | ManyToManyField |  |  | → `orders.Categories` |
| `main_groups` | ManyToManyField |  |  | → `users.MainGroup` |
| `states` | ManyToManyField |  |  | → `users.State` |

### `UserPartyAssignment` → `user_party_assignments`  ordering=['-assigned_at']

> Maps users to parties using card_code


| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `card_code` | CharField |  | Y | len 50 |
| `category` | CharField | Y | Y | choices: OIL, BEVERAGES, MART; len 20 |
| `assigned_at` | DateTimeField |  |  |  |
| `assigned_by` | ForeignKey | Y | Y | → `users.User` SET_NULL |
| `is_active` | BooleanField |  |  | default=True |

- **unique_together**: [['user', 'card_code', 'category']]

### `UserRole` → `users_role`  ordering=['name']

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `name` | CharField |  |  | unique; len 50 |
| `display_name` | CharField |  |  | len 100 |
| `is_active` | BooleanField |  |  | default=True |

### `UserState` → `users_user_states`  **unmanaged**

| field | type | null | idx | notes |
|---|---|---|---|---|
| `id` | AutoField |  |  | unique |
| `user` | ForeignKey |  | Y | → `users.User` CASCADE |
| `state` | ForeignKey |  | Y | → `users.State` CASCADE |


---

**Totals: 98 models, 1015 fields.**
