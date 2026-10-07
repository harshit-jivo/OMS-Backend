# Credit Limit (CREDIT_LIMIT) — replacing JSAP's credit-limit approval

OMS now owns credit-limit approval end to end. JSAP's `cl` schema is no longer
written or read for new requests.

| JSAP | OMS |
|---|---|
| `cl.jsCreditDocument` | `credit_limit.credit_limit_request` |
| `cl.jsFlow` | `credit_limit.credit_limit_flow` |
| `cl.jsFlowStatus` | `credit_limit.credit_limit_action_log` |
| `jsTemplate` / `jsQuery` (type 12) / `jsStage` / `jsUserStage` | `/Workflows`, module `CREDIT_LIMIT` |
| trigger `trg_AutoCreateCreditDocumentWorkflow` (first match wins) | `selection.select_for_module` (lowest `priority` wins, a tie is an error) |
| JSAP API writes `OCRD.CreditLine` as `B1i` | final approval: `PATCH /BusinessPartners('<code>') {CreditLimit}` |

## Behaviour

- **One submission, many parties.** `POST requests/` takes a company, a
  `lines` list (`card_code`, `new_credit_limit`, `valid_till` per party, up to
  50), and shared `remarks` and `attachments`. Each line becomes its own
  request on its own approval chain. All or nothing: if any line cannot be
  raised, nothing is saved and the 409 lists the failing lines
  (`errors.lines: [{index, card_code, message}]`).
- **Supporting documents:** up to 10 per submission (`attachments`, sent as
  repeated multipart fields). At least one is required for a single party;
  for several they are optional (`flow.attachment_required`). Each file is
  stored once and listed on every request in the submission
  (`credit_limit.credit_limit_attachment`, one row per request per file).
  Download: `GET requests/<pk>/attachments/<id>/`. Invoice review always
  raises one party, so it always requires at least one.
- **Customer facts come from SAP.** At submission the server reads `CardName`,
  `U_Main_Group`, `Balance` and `CreditLine` from OCRD. The client sends only
  company, card code, new limit, valid-till, remarks and the attachment.
- **Routing failures are refused.** A request no workflow matches (or two
  match at the same priority) is not stored. JSAP silently kept 30 such rows.
- **SAP is the gate on the final approval.** The limit is written first; a
  refusal rolls the approval back, the request stays pending at the last
  stage, and SAP's answer is stored on the flow.
- **The commitment limit is raised with it.** The Service Layer refuses any
  `CreditLimit` above `OCRD.DebtLine` (error -5002, verified on the TEST
  company — even for customers already above it), so the PATCH also sends
  `MaxCommitment = max(current DebtLine, new limit)`. It is never lowered.
- **Rejection is final.** Raise a new request.
- **`valid_till` is recorded, not enforced.** JSAP never reverted a limit either
  (verified against OCRD history).
- **Keys:** `Credit_Limit` (raise / view own), `Credit_Limit_Approval` (desk).
  Acting also requires being the current stage's effective user.

Invoice review keeps its "Raise CL" / "Show Flow" buttons; they now create and
read OMS requests. Invoices with a request raised in JSAP before cutover still
show their JSAP flow (`invoice/views.py`, marked LEGACY) until JSAP is off.

## Configuring the workflows (translated from the live JSAP templates)

Queries return `id` from `credit_limit.credit_limit_request`. Columns:
`company`, `card_code`, `main_group`, `current_balance`, `current_credit_limit`,
`new_credit_limit`, `created_by_id`.

With `priority`, the special-customer workflows sit at a lower number than the
general one, so the general query no longer needs JSAP's `NOT IN` lists.

| Workflow | Company | Priority | Query `WHERE` | Stages (JSAP user) |
|---|---|---|---|---|
| CL Oil — internal | OIL | 10 | `card_code IN ('CUSTA000680','CUSTA000236','CUSTA000356','CUSTA000844')` | Preshit (101) |
| CL Oil — Akal / Avtar | OIL | 10 | `card_code IN ('CUSTA000242','CUSTA000175')` | Avtar Singh (72) |
| CL Oil — general | OIL | 100 | `company = 'OIL' AND (main_group <> 'STAFF' OR new_credit_limit > 5000)` | Gurvinder (78) → Gurpreet (79) |
| CL Beverages — internal | BEVERAGES | 10 | `card_code IN ('CUSTA000680','CUSTA000236','CUSTA000356','CUSTA000844')` | Preshit (101) |
| CL Beverages — Akal / Avtar | BEVERAGES | 10 | `card_code IN ('CUSTA000242','CUSTA000175')` | Avtar Singh (72) |
| CL Beverages — general | BEVERAGES | 100 | `company = 'BEVERAGES' AND (main_group <> 'STAFF' OR current_balance > 5000)` | Manjeet (221) → Gurpreet (79) |
| CL Mart | MART | 100 | `company = 'MART'` | Navdeep (140) → Prabhjot (76) |

Full query form: `SELECT id FROM credit_limit.credit_limit_request WHERE <above>`.

Decisions to make while configuring:

- **STAFF customers at or under 5000 have no workflow in JSAP**, so those
  requests will be refused with "no workflow configured" until one is added.
  The STAFF rule is also now meaningful for BEVERAGES: JSAP's DSR app sent
  `'C'` there instead of the group, so that rule never fired.
- **JSAP templates 231 / 406 / 407** (requests created by SAPUSER, user 97)
  were IT test routes and are not translated.
- Approvers other than Gurvinder have no OMS user with a matching email yet —
  create them before adding the stages.

## Not covered

- The DSR mobile app raised most requests through JSAP's API (user 131: 1,060
  since June 2026). It must call `POST /api/credit-limit/requests/` instead, or
  those users raise requests on `/Credit_Limit`.
- No JSAP history is imported.
