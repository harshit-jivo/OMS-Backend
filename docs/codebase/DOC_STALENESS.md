# Documentation staleness

The repo carries ~15,500 lines of pre-existing `.md`. Some of it has
drifted from the code. This ranks which docs to distrust.

Two objective signals — no guessing about prose:

- **Dangling refs** — backtick-quoted source paths that no longer exist.
  A doc describing files that are gone is describing a codebase that is gone.
- **Behind by** — days between the doc's last commit and the last commit
  to the app it mostly describes. Large gaps mean the code moved on.

Neither proves a doc is wrong. Both say: re-read before trusting.

| doc | lines | mostly about | behind by | dangling refs |
|---|---:|---|---:|---|
| `docs/RELEASE.md` | 1232 | OMS | 39d | `OMS-app/app.json`, `OMS-app/eas.json`, `OMS-app/package.json` +4 ⚠️ |
| `docs/ap-invoice-service-layer.md` | 260 | invoice | current | `ap_attach.py`, `ap_flow.py`, `ap_post.py` +4 ⚠️ |
| `README.md` | 170 | orders | 100d | — |
| `docs/overview.md` | 258 | OMS | 58d | — |
| `docs/db_schema_diagrams.md` | 507 | orders | 53d | — |
| `docs/developer-onboarding-checklist.md` | 507 | orders | 53d | — |
| `docs/hana-module-workflow.md` | 259 | hana | 53d | — |
| `docs/invoice-module-workflow.md` | 204 | invoice | 53d | — |
| `docs/orders-module-workflow.md` | 306 | orders | 53d | — |
| `docs/project-architecture-and-express-mapping.md` | 101 | OMS | 53d | — |
| `docs/sap_sync-module-workflow.md` | 453 | sap_sync | 53d | — |
| `docs/serviceLayer-module-workflow.md` | 191 | OMS | 53d | — |
| `docs/users-module-workflow.md` | 353 | OMS | 53d | — |
| `docs/notification.md` | 367 | OMS | 47d | — |
| `docs/push-token-cleanup.md` | 289 | OMS | 39d | — |
| `docs/web-push-cleanup.md` | 184 | OMS | 39d | — |
| `docs/audit-module-workflow.md` | 170 | audit | 33d | — |
| `docs/APPROVAL_API.md` | 755 | approvals | 21d | — |
| `docs/SAP_VERIFICATION.md` | 654 | OMS | 21d | — |
| `docs/DEPOSIT_API.md` | 629 | payments | 19d | — |
| `docs/PAYMENT_API.md` | 936 | payments | 19d | — |
| `payments/README.md` | 162 | payments | 19d | — |
| `docs/scheme-architecture.md` | 349 | orders | 15d | — |
| `einvoice/docs/INVOICE_PRINTING_AND_QR_RUNBOOK.md` | 379 | OMS | 13d | — |
| `tracker/docs/TRACKER_SYSTEM.md` | 720 | invoice | 13d | — |
| `docs/CHEQUE_RECEIPT_SAP_EVIDENCE.md` | 221 | OMS | 12d | — |
| `docs/PHASE_3_3_MODEL_MIGRATION_PLAN.md` | 408 | orders | 12d | — |
| `docs/PAYMENTS_SAP_FLOW_REDESIGN.md` | 291 | payments | 9d | — |
| `docs/LIVE_SAP_OMS_ACCOUNTING_AUDIT.md` | 416 | OMS | 4d | — |
| `docs/PAYMENTS_DEPOSITS_FLOW_EXPLAINED.md` | 616 | OMS | 4d | — |
| `docs/file-upload-utility.md` | 417 | users | 1d | — |
| `docs/BRANCH_MERGE_2026-08-26.md` | 639 | orders | current | — |
| `docs/legal-module-workflow.md` | 137 | legal | current | — |
| `docs/NOTIFICATION_INTEGRATION.md` | 744 | notifications | current | — |
| `docs/SKU-module-workflow.md` | 143 | SKU | current | — |
| `docs/NOTIFICATION_PHASE3_IMPLEMENTATION_BLUEPRINT.md` | 398 | notifications | current | — |
| `einvoice/docs/NIC_EINVOICE_EWAYBILL_REFERENCE.md` | 669 | invoice | current | — |

**37 docs. 2 name at least one file that no longer exists. 16 are more than 30 days behind the app they describe.**
