-- ===========================================================================
--  PRDO test wiring — TEST_JIVO_OIL_HANADB ONLY
--  ---------------------------------------------------------------------
--  Moves the production-order release gate off JSAP's PRODUCTIONORDERSYNC
--  and onto an OMS-owned table, in the TEST company database only.
--
--  DO NOT RUN THIS AGAINST JIVO_OIL_HANADB.
--
--  Rollback is at the bottom of this file. The original TEST procedure is
--  backed up beside this script as TN_TEST_OIL.backup.sql (28 lines, the
--  stock SAP template — the TEST company had no gate at all before this).
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. The OMS-owned approval table
--
-- One row per approved production order. The schema IS the company, so
-- DocEntry alone is the key here (unlike the OMS Postgres side, where one
-- table holds all three companies and the key is (company, sap_doc_entry)).
--
-- `Company` is kept for traceability only, never joined on. JSAP's equivalent
-- column was an integer that was wrong for all 11 of its Beverages rows.
--
-- SAP reads exactly two columns: DocEntry (to find the row) and Status (to
-- see if it is 'A'). Everything else is OMS audit.
--
-- `DecidedBy` / `DecidedAt`, NOT `ApprovedBy` / `ApprovedAt`: OMS writes both
-- outcomes here, and a rejected row reading "ApprovedBy: preshit" would say
-- the opposite of what happened. A rejection could have been left as no row
-- at all — the gate tests `<> 'A'` — but then "no row" would mean both
-- *rejected* and *nobody has looked at it*, which nothing reading this table
-- from inside SAP could tell apart.
-- ---------------------------------------------------------------------------
CREATE COLUMN TABLE "TEST_JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL" (
    "DocEntry"    INTEGER       NOT NULL,   -- OWOR.DocEntry
    "Status"      NVARCHAR(1)   NOT NULL,   -- 'A' approved / 'P' pending / 'R' rejected
    "Company"     NVARCHAR(20)  NOT NULL,   -- traceability only
    "RequestId"   BIGINT,                   -- production.production_order.id
    "DecidedBy"   NVARCHAR(100),            -- OMS username, audit only
    "DecidedAt"   TIMESTAMP,
    "SyncedAt"    TIMESTAMP     NOT NULL,
    PRIMARY KEY ("DocEntry"),
    CONSTRAINT "OMS_PRDO_APPROVAL_STATUS_CHK" CHECK ("Status" IN ('A','P','R'))
);


-- ---------------------------------------------------------------------------
-- 2. Seed from the existing JSAP table
--
-- So that switching the gate changes NOTHING about which orders are blocked.
-- The only variable under test is which table the rule reads. Without this
-- seed every historical order would become un-releasable overnight, which
-- would test nothing and break the test company.
--
-- Note PRODUCTIONORDERSYNC."DOCENTRY" is NVARCHAR, so it is cast.
-- ---------------------------------------------------------------------------
INSERT INTO "TEST_JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL"
       ("DocEntry", "Status", "Company", "RequestId", "DecidedBy", "DecidedAt", "SyncedAt")
SELECT TO_INT("DOCENTRY"),
       "STATUS",
       'OIL',
       NULL,
       'migrated-from-jsap',
       "UPDATEDON",
       CURRENT_TIMESTAMP
FROM   "TEST_JIVO_OIL_HANADB"."PRODUCTIONORDERSYNC"
WHERE  "DOCENTRY" IS NOT NULL
  AND  "STATUS" IN ('A','P','R');


-- ---------------------------------------------------------------------------
-- 3. The gate
--
-- The TEST procedure was the stock 28-line SAP template with
-- "ADD YOUR CODE HERE" and no rules at all, so this ADDS a block rather than
-- editing one. Live's 22,792-line version is untouched and unaffected.
--
-- Differences from the live OIL block, both deliberate:
--
--   * reads OMS_PRDO_APPROVAL instead of PRODUCTIONORDERSYNC  <- the point
--   * sets `error` / `error_message` instead of emitting a bare
--     `SELECT code, msg FROM DUMMY`. Both work; the variable form is the
--     documented SAP pattern and leaves no ambiguity about which result set
--     SAP reads when several blocks fire.
--
-- The `UserSign != 33` exemption is KEPT so this is a like-for-like rehearsal.
-- It is on its own line, marked, because it is the line that makes the gate
-- apply to only 8% of production. Delete it to see the gate actually fire.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE "TEST_JIVO_OIL_HANADB"."SBO_SP_TRANSACTIONNOTIFICATION" (
    in object_type              nvarchar(30),   -- SBO Object Type
    in transaction_type         nchar(1),       -- [A]dd [U]pdate [D]elete [C]ancel c[L]ose
    in num_of_cols_in_key       int,
    in list_of_key_cols_tab_del nvarchar(255),
    in list_of_cols_val_tab_del nvarchar(255)
)
LANGUAGE SQLSCRIPT AS
    error         int;              -- 0 = no error
    error_message nvarchar(200);
BEGIN
    error := 0;
    error_message := N'Ok';

    ---------------------------------------------------------------------
    -- Production Order release gate  (object type 202)
    ---------------------------------------------------------------------
    IF :object_type = N'202' AND (:transaction_type = N'A' OR :transaction_type = N'U') THEN
        IF EXISTS (
            SELECT 1
            FROM        "OWOR" A
            LEFT JOIN   "OMS_PRDO_APPROVAL" P ON P."DocEntry" = A."DocEntry"
            INNER JOIN  "OITM" O ON O."ItemCode" = A."ItemCode"
            WHERE  A."DocEntry" = TO_INT(:list_of_cols_val_tab_del)
              AND  A."Status"   = 'R'                              -- being Released
              AND  A."Type"     = 'S'                              -- Standard only
              AND  (P."Status" <> 'A' OR P."Status" IS NULL)       -- not approved in OMS
              AND  O."Series"  <> 392                             -- raw materials exempt
              AND  A."UserSign" <> 33                              -- <<< THE EXEMPTION
        ) THEN
            error := 202264;
            error_message := N'Production order is not approved in OMS. Get it approved before releasing it.';
        END IF;
    END IF;

    -- Return values
    SELECT :error, :error_message FROM dummy;
END;


-- ---------------------------------------------------------------------------
-- 4. Applied later: the columns were renamed once OMS started writing
--    rejections as well as approvals, so the table records a DECISION rather
--    than only an approval. Already applied to TEST; here for the record and
--    for whatever schema goes next.
-- ---------------------------------------------------------------------------
-- RENAME COLUMN "TEST_JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL"."ApprovedBy" TO "DecidedBy";
-- RENAME COLUMN "TEST_JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL"."ApprovedAt" TO "DecidedAt";


-- ===========================================================================
-- ROLLBACK
-- ===========================================================================
-- Restores the stock template and removes the table. Run both.
--
--   DROP TABLE "TEST_JIVO_OIL_HANADB"."OMS_PRDO_APPROVAL";
--
--   CREATE OR REPLACE PROCEDURE "TEST_JIVO_OIL_HANADB"."SBO_SP_TRANSACTIONNOTIFICATION" (
--       in object_type nvarchar(30), in transaction_type nchar(1),
--       in num_of_cols_in_key int, in list_of_key_cols_tab_del nvarchar(255),
--       in list_of_cols_val_tab_del nvarchar(255) )
--   LANGUAGE SQLSCRIPT AS
--       error int; error_message nvarchar(200);
--   BEGIN
--       error := 0; error_message := N'Ok';
--       SELECT :error, :error_message FROM dummy;
--   END;
