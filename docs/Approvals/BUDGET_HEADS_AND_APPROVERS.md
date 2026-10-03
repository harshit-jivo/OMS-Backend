# Budget heads, departments and approvers — as configured in JSAP

Reference extract from `jsaplive3`, 2026-09-21. Two things, kept separate:
**§1** the budget heads and the departments under each, **§2** who currently
approves them.

Companion to `BUDGET_JSAP_SAP.md`, which explains how the mechanism works.

> **Coverage: OIL and BEVERAGE only.** Mart has **no budget approval in JSAP** —
> zero rows in `bud.Budgets`, zero templates, and `bud.jsBudgetTable` contains
> only the two branches. Anything below that says "all companies" means these
> two.

A **budget head** is the spending bucket a cost is charged to and the level a
monthly budget is set at. It is the SAP **Dimension-3 cost centre**
(`OPRC` where `DimCode = 3`), mirrored into `bud.Budgets.budgetName` and stamped
on each document line as `bud.jsBudgetTable.BUDGET`. The **department** is the
level below it — `SUB_BUDGET`, from SAP Dimension 4.

---

## 1. Budget heads and their departments

Two lists per company, because they disagree: what the **master** (`bud.SubBudgets`)
declares, and what documents are **actually tagged** with. Line counts and
amounts are the whole history in `bud.jsBudgetTable`.

### 1.1 OIL

| # | Budget head | Departments in the master | Departments actually used (lines) |
|---|---|---|---|
| 1 | **BackOff** (Back Office) | Accounts, Admin, HR_DEPT, IMPORT, IT, Legal | Admin 2384, IT 958, Legal 371, Accounts 275, HR_DEPT 76, IMPORT 50 |
| 2 | **Del Bkhp** (Delivery Bhakharpur) | GT, MT, ROI | *(blank)* 7511, ROI 5, GT 3, MT 2 |
| 3 | **FACT_COM** (Factory Common) | Trm Loan | *(blank)* 2342, Trm Loan 3, GP-GDWN 2, PLANT 2, Accounts 1 |
| 4 | **Factory** | Admin, EXPORT, GP-GDWN, GT, IMPORT, PLANT, Trm Loan | *(blank)* 2599, GP-GDWN 11, PLANT 7, EXPORT 7, Trm Loan 3, IMPORT 1 |
| 5 | **Interest** | BankChgs, CC Limit, TAXATION, Trm Loan, VHCLLOAN | BankChgs 2713, CC Limit 326, Trm Loan 118, VHCLLOAN 73, TAXATION 4 |
| 6 | **Med MKT** (Media Marketing) | DIG MKT, DIGTAL M, POP, PPR MED, SOCIAL M | DIGTAL M 311, POP 102, SOCIAL M 2 |
| 7 | **NPD1** | — | *(blank)* 203, Legal 1 |
| 8 | **NPD2** | — | *(blank)* 153 |
| 9 | **NPD3** | — | *(blank)* 22 |
| 10 | **OTE** (One Time Expense) | Accounts, Admin, GT | *(blank)* 386, GT 3, MT 1, Accounts 1, CSD 1 |
| 11 | **R & D** | — | *(blank)* 3 |
| 12 | **Sales** | CAL CNTR, CSD, E-COM, EXPORT, GT, HORECA, MIS, MT, ROI | CSD 424, GT 345, MT 227, E-COM 181, ROI 128, CAL CNTR 67, EXPORT 52, HORECA 20, MIS 19 |
| 13 | **Sales RE** (Sales Realisation) | CSD, E-COM, GT, HORECA, MT, ROI | E-COM 1143 |
| 14 | **Transprt** (Transport) | — | *(not separately tagged)* |
| — | **Del Mayp** (Delivery Mayapuri) | **not in master** | *(blank)* 28, VHCLLOAN 1 |
| — | **JIVOS** | **not in master** | *(blank)* 50, Admin 1 |

### 1.2 BEVERAGE

The master has **no departments at all** for Beverage — all 11 heads have zero
`SubBudgets` rows — yet documents are tagged with departments anyway.

| # | Budget head | Departments in the master | Departments actually used (lines) |
|---|---|---|---|
| 1 | **BackOff** | — | Legal 40, Admin 22, IT 6, Accounts 1, IMPORT 1 |
| 2 | **Factory** | — | *(blank)* 1512, GT 6, Admin 1 |
| 3 | **Interest** | — | BankChgs 279, CC Limit 1 |
| 4 | **Med MKT** | — | DIG MKT 113, PPR MED 22 |
| 5 | **NPD2** | — | *(not tagged)* |
| 6 | **NPD3** | — | *(not tagged)* |
| 7 | **OTE** | — | *(blank)* 16, Admin 2 |
| 8 | **R & D** | — | *(not tagged)* |
| 9 | **Sales** | — | GT 145, CSD 29, MT 21, E-COM 3, HORECA 2, EXPORT 1 |
| 10 | **Sales RE** | — | GT 4 |
| 11 | **Transprt** | — | *(not tagged)* |
| — | **Del Bkhp** | **not in master** | *(blank)* 6083 |
| — | **FACT_COM** | **not in master** | *(blank)* 71, GT 2, EXPORT 1 |

### 1.3 The full department vocabulary

Every distinct `SUB_BUDGET` in use, across both companies:

```
Accounts   Admin      BankChgs   CAL CNTR   CC Limit   CSD       DIG MKT
DIGTAL M   E-COM      EXPORT     GP-GDWN    GT         HORECA    HR_DEPT
IMPORT     IT         Legal      MIS        MT         PLANT     POP
PPR MED    ROI        SOCIAL M   TAXATION   Trm Loan   VHCLLOAN
```

### 1.4 Problems visible in this list

1. **Four heads are used but have no master row** — OIL `Del Mayp`, `JIVOS`;
   BEVERAGE `Del Bkhp`, `FACT_COM`. A head with no `bud.Budgets` row can never
   resolve a monthly allocation, so the auto-approver can never clear its
   documents. `Del Bkhp` alone carries 6,083 Beverage lines.
2. **Beverage has no department master at all** — 11 heads, zero `SubBudgets`
   rows, but documents tagged `Legal`, `DIG MKT`, `GT`, `CSD` and so on. The
   department layer exists only in the data.
3. **Most volume carries no department.** `Del Bkhp` 7,511 of 7,521 OIL lines
   are blank; `Factory` 2,599 of 2,628; `FACT_COM` 2,342 of 2,350. For those
   heads the department level is effectively unused.
4. **Master departments that are never used**: OIL `Med MKT` declares `DIG MKT`
   and `PPR MED` but documents use `DIGTAL M` and `POP`; `Del Bkhp` declares
   GT/MT/ROI but 99.9% of its lines are blank; `Sales RE` declares six but only
   `E-COM` appears.
5. **`Transprt`, `R & D`, `NPD2`, `NPD3`** have essentially no tagged spend,
   though `Transprt` and `R & D` both have an active approval template.

---

## 2. Current approvers in JSAP

From the active budget templates (`jsTemplate.isActive = 1` joined to
`jsTemplateApproval.approvalId = 5`) and their stages.

### 2.1 The people

| userId | login | name | email | OIL templates | BEV templates |
|---|---|---|---|---|---|
| 78 | `gurvinder` | Gurvinder Singh | gurvinder@jivo.in | 14 (+1 at stage 2) | 10 (+1 at stage 2) |
| 69 | `jasbirsingh` | Jasbir Singh | jasbir@jivo.in | 12 | 6 |
| 95 | `arsh` | Arshdeep Singh | arsh@jivo.in | 8 | 8 |
| 72 | `avtarsingh` | Avtar Singh | avtar@jivo.in | 7 | 7 |
| 87 | `karanpreet` | Karanpreet Singh | karanpreet@jivo.in | 5 | 3 |
| 79 | `gurpreetsingh` | Gurpreet Singh | gs@jivo.in | 4 *(stage 2 only)* | 4 *(stage 2 only)* |
| 71 | `ravindersingh` | Ravinder Singh | rs@jivo.in | 4 | 4 |
| 80 | `arvinder` | Arvinder Singh | arvinder@jivo.in | — | 6 |
| 75 | `gagan` | Gagandeep Singh | gagandeep@jivo.in | 1 | — |
| 76 | `prabhjot` | Prabhjot Singh | ps@jivo.in | 1 | — |

Ten people in total; nine touch OIL, nine touch BEVERAGE.

### 2.2 Which head goes to whom

**OIL**

| Budget head | Stage 1 | Stage 2 |
|---|---|---|
| Sales, Sales RE | `gurvinder` | — |
| Factory, FACT_COM, Del Bkhp | `jasbirsingh` | — |
| Del Mayp | `jasbirsingh`, `prabhjot` *(JV only)* | — |
| BackOff — IT, HR_DEPT, Accounts, Admin, IMPORT | `ravindersingh` | — |
| BackOff — Legal | `karanpreet` | — |
| Interest | `avtarsingh` | — |
| Med MKT, Transprt, R & D | `arsh` | — |
| OTE | `arsh`; `karanpreet` *(Legal)* | — |
| JIVOS, NPD1, NPD2, NPD3 | `avtarsingh` | `gurpreetsingh` |
| *(no filter — template `t1`)* | `gagan` | — |

**BEVERAGE**

| Budget head | Stage 1 | Stage 2 |
|---|---|---|
| Sales, Sales RE | `gurvinder` | — |
| Factory | `arvinder`, `jasbirsingh` | — |
| FACT_COM | `arvinder` | `gurvinder` |
| Del Bkhp, Del Mayp | `jasbirsingh` | — |
| BackOff — general / Legal | `ravindersingh` / `karanpreet` | — |
| Interest | `avtarsingh` | — |
| Med MKT, OTE, Transprt | `arsh` | — |
| JIVOS, NPD1, NPD2 | `avtarsingh` | `gurpreetsingh` |

Almost every template is **single-stage, single-approver**. Only the NPD/JIVOS
chain (`avtarsingh` → `gurpreetsingh`) and Beverage `FACT_COM`
(`arvinder` → `gurvinder`) have two.

### 2.3 How a head is split into several templates

A head does not map to one template. Each is split by document type and account
class, and each split routes independently:

| suffix | meaning | filter |
|---|---|---|
| `… oil v5` | ordinary spend | `ObjType != 28`, salary accounts excluded |
| `… ws oil v4` | salary | `ObjType = 28` + `AcctCode in (5630001…5630016)` |
| `jv … oil v8` | journal voucher, non-salary | `ObjType = 28`, salary accounts excluded |
| `ele …` | electricity | `AcctCode in (5680011)` |

So `BackOff` alone has six active templates. OIL has **52** budget templates
across 14 heads; the query set that drives them is 51.

### 2.4 Problems visible in the approver list

1. **Template `t1` (OIL) has no filter at all** — no `BUDGET`, no `SUB_BUDGET`,
   any document type — and routes to `gagan`. The engine has no tie-breaking, so
   a filterless query is an ambiguity risk against every other template. It has
   0 pending, so it is not currently matching, but it looks unfinished rather
   than deliberate.
2. **Template `sales oil v8` (id 273) has a stage but no query**, so it can
   never match a document. Its stage-2 user is `gurvinder`. Dead configuration.
3. **`prabhjot` and `gagan` hold one template each**; `gurpreetsingh` only ever
   appears at stage 2. Three of the ten are effectively edge cases.
4. **The approvers are not the SAP cost-centre owners.** SAP records an owner
   and co-owner per Dimension-3 cost centre (`OPRC.CCOwner` / `U_Co_Owner`) and
   they disagree with this table — e.g. OIL `Sales` is owned by Gurvinderjeet
   Singh in SAP and approved by `gurvinder` here, but `Factory` is owned by
   Bhupinder Singh and approved by `jasbirsingh`. Two sources of truth for the
   same question.

---

## 3. If this is re-pointed at the new vertical structure

The six verticals (Oil Sales, Oil Production & Supply Chain, Infra Development
& Plant Support, Beverages Production & Sales, Ecom Sales, Ancillary Services)
are a layer **above** the budget head, and they do not exist in SAP or JSAP
today. Mapping them means grouping the heads in §1 and deciding, per head, which
vertical owns it.

Two facts to settle first:

- **Only some vertical heads currently approve anything.** Gurvinder, Gagan and
  Arvinder appear above; the people named for Oil Sales, Ecom and Ancillary
  Services either hold no templates or could not be matched to a JSAP user.
- **Mart has no budget approval at all**, so vertical 5 (Ecom Sales / Jivo Mart)
  has nothing to attach to until it is set up from scratch.
