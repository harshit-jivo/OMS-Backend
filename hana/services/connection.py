"""Direct HANA reads.

Values reach these queries as BIND PARAMETERS. They did not used to: 24 of the
builders below interpolated their arguments straight into the SQL string, and
only 8 applied any escaping at all — a hand-rolled `.replace("'", "''")` per
call site. `get_customer_details` took `request.query_params.get('card_code')`
and dropped it into `WHERE T0."CardCode" = '{party_code}'`.

`payments/hana_queries.py` had already written the diagnosis down:

    "Unlike the 21 builders in hana/services/connection.py, every value here is
     a BIND PARAMETER. HANAConnection.execute(sql, params) has always supported
     them; they were simply never used, which is why get_customer_details
     interpolates request query params straight into SQL."

Two rules follow, and they are the whole convention:

1. **Every value binds.** Use `?` and pass the value in `params`. Never format
   a value into the SQL, however sure you are of where it came from — the
   escaping that existed was correct, and it was correct at 8 sites out of 32.

2. **Only a schema name may be interpolated**, because HANA cannot bind an
   identifier. It must come from `_schema_for_branch`, which resolves against
   settings and refuses anything else — never from a request.

Builders that take a value return `(sql, params)`; `execute` accepts that pair
directly, so callers in `services.py` are unchanged.
"""
import logging

from hdbcli import dbapi
from django.conf import settings

logger = logging.getLogger(__name__)


class HanaSchemaError(ValueError):
    """A branch did not resolve to a configured HANA schema.

    Raised rather than defaulted. The three company databases hold different
    documents under the same DocNum, so quietly falling back to OIL does not
    fail — it returns another company's data, which is the failure mode
    `docs/CODEBASE_AND_REFACTOR_PLAN.md` §1.1 calls out as the single most
    important fact about this system.
    """


class HANAConnection:
    def __init__(self):
        self.connection = None
        self.cursor = None
        
    def connect(self):
        cfg =  settings.DATABASES['hana']
        try :
            self.connection = dbapi.connect(
                address=cfg['HOST'],
                port=int(cfg['PORT']),
                user=cfg['USER'],
                password=cfg['PASSWORD'],
            )
            self.cursor =  self.connection.cursor()
            # NEVER print() here. Under nssm this process has a stdout pipe
            # that nothing reads (AppStdout is unset), so a write to it raises
            # OSError [WinError 233] "No process is on the other end of the
            # pipe". That is not hypothetical: these two lines used to be
            # print() calls, and they broke EVERY HANA-backed feature in the
            # deployed service while working perfectly in a dev terminal,
            # which has a real console attached.
            #
            # Worse, the print in the except branch raised the same OSError
            # while handling the first one, which discarded the ConnectionError
            # below and let a bare OSError escape — so the logged cause read
            # "No process is on the other end of the pipe" and pointed
            # investigation at SAP, which was healthy the whole time.
            logger.debug('Connected to HANA at %s:%s', cfg['HOST'], cfg['PORT'])
            return True
              
        except Exception as e:
            # No password in this message: cfg['PASSWORD'] is deliberately not
            # interpolated.
            logger.warning('HANA connect failed for %s@%s:%s: %s',
                           cfg.get('USER'), cfg.get('HOST'), cfg.get('PORT'), e,
                           exc_info=True)
            raise ConnectionError(f"SAP connection failed: {str(e)}")
        
    def disconnect(self):
        if self.cursor:
            self.cursor.close()
            self.cursor = None
        
        if self.connection:
            self.connection.close()
            self.connection = None
    
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        
    def execute(self, sql, params=None):
        """Run a query. `sql` may be a string, or the `(sql, params)` pair the
        parameterised builders return.

        Accepting the pair is what let the builders move to bind parameters
        without touching all 29 call sites in `services.py` — each one passes
        `Queries.x(...)` straight through, and now carries its values with it.
        """
        if isinstance(sql, tuple):
            if params is not None:
                raise TypeError(
                    'execute() got params both in the (sql, params) pair and '
                    'as an argument; pass them one way or the other.')
            sql, params = sql

        try:
            self.cursor.execute(sql, params or [])
            if self.cursor.description is None:
                self.connection.commit()
                return []

            columns = [col[0] for col in self.cursor.description]
            return [dict(zip(columns, row)) for row in self.cursor.fetchall()]

        except Exception as e:
            self.connection.rollback()
            
            raise RuntimeError(f"HANA query failed: {str(e)}")



#: (schema, table, column) -> exists. A SAP user-defined field is created by an
#: administrator in the SAP client, not at runtime, so caching for the life of
#: the process is safe; a restart picks up a newly added one.
_COLUMN_EXISTS_CACHE = {}


def column_exists(conn, schema, table, column):
    """Whether `schema`.`table` actually has `column`.

    SAP user-defined fields are PER COMPANY DATABASE. `U_OMS_REF` was added to
    the oil company's ORDR and ODRF and to neither of the others, so a query
    naming it succeeds against OIL and fails against BEVERAGES with

        invalid column name: T0.U_OMS_REF

    That is what took the beverage half of the SO vs AR Invoice report out
    entirely: the view turns any HANA error into a 502, and the page renders
    the empty result as "no orders" rather than as a failure.

    Checking beats hardcoding `if branch == 'BEVERAGE'`: the field can be added
    to another company at any time by someone who will not think to edit this
    file, and the report should start showing it when they do — and stop if it
    is ever removed from OIL.
    """
    key = (schema, table, column)
    if key not in _COLUMN_EXISTS_CACHE:
        rows = conn.execute(
            'SELECT 1 AS "found" FROM SYS.TABLE_COLUMNS '
            'WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?',
            [schema, table, column],
        )
        _COLUMN_EXISTS_CACHE[key] = bool(rows)
    return _COLUMN_EXISTS_CACHE[key]


class Queries():
    OIL_SCHEMA = settings.DATABASES['hana']['OIL_SCHEMA']
    BEVERAGE_SCHEMA = settings.DATABASES['hana']['BEVERAGE_SCHEMA']
    # Third company. Only the DocNum -> DocEntry lookup (bill print) is wired for
    # it so far; the other queries below are still OIL/BEVERAGE only.
    MART_SCHEMA = getattr(settings, 'HANA_MART_COMPANY_DB', '')

    @staticmethod
    def _schema_for_branch(branch):
        """The HANA schema for a branch, or raise.

        Replaces the `if branch == 'OIL': s = ... elif branch == 'BEVERAGE': s
        = ...` pattern repeated through this class, which left `s` UNBOUND for
        any other value — so an unexpected branch raised `UnboundLocalError`
        from inside a query builder rather than saying what was wrong.

        This is also the one place a schema name may be chosen. Values bind;
        identifiers cannot, so an identifier must come from settings and be
        checked against the configured set before it is formatted into SQL.
        """
        key = str(branch or '').strip().upper()
        schemas = {
            'OIL': Queries.OIL_SCHEMA,
            'BEVERAGE': Queries.BEVERAGE_SCHEMA,
            'BEVERAGES': Queries.BEVERAGE_SCHEMA,
            'MART': Queries.MART_SCHEMA,
        }
        schema = str(schemas.get(key) or '').strip()
        if not schema:
            raise HanaSchemaError(
                f'Unknown or unconfigured branch {branch!r}. '
                f'Expected one of: {", ".join(sorted(schemas))}.')
        return schema

    @staticmethod
    def _open_so_schemas():
        """All configured SAP company DBs (OIL / BEVERAGES / MART), de-duplicated.

        Open sales orders live in a different company DB per category, so any
        query that looks up open SOs must search across all of them. Still used
        by the open-SO lookups further down this class.
        """
        configured = [
            getattr(settings, 'HANA_OIL_COMPANY_DB', '') or Queries.OIL_SCHEMA,
            getattr(settings, 'HANA_BEVERAGE_COMPANY_DB', '') or Queries.BEVERAGE_SCHEMA,
            getattr(settings, 'HANA_COMPANY_DB_MART', ''),
        ]
        schemas = []
        seen = set()
        for schema in configured:
            schema = str(schema or '').strip()
            if schema and schema not in seen:
                seen.add(schema)
                schemas.append(schema)
        return schemas

    @staticmethod
    def get_product_stock(branch):
        # Branch-scoped: one company DB per call ('OIL' / 'BEVERAGE'), as called
        # from hana/services/services.py. Kept as a (schema, category) list so the
        # query body below is unchanged.
        if branch == 'BEVERAGE':
            unique_schemas = [(Queries.BEVERAGE_SCHEMA, 'BEVERAGES')]
        else:
            unique_schemas = [(Queries.OIL_SCHEMA, 'OIL')]

        item_filter = """
            (
                T0."ItemCode" LIKE 'FG%' OR
                T0."ItemCode" LIKE 'SCH%' OR
                T0."ItemCode" LIKE 'RM%' OR
                T0."ItemCode" LIKE 'PM%' OR
                T0."ItemCode" LIKE 'SC%'
            )
        """

        queries = [
            f"""
            SELECT
                T0."ItemCode" AS "item_code",
                T0."ItemName" AS "item_name",
                '{category}' AS "category",
                T0."SalFactor2" AS "sal_factor2",
                T0."U_Rev_tax_Rate" AS "tax_rate",
                T0."Deleted" AS "is_deleted",
                T0."U_Variety" AS "variety",
                T0."U_TYPE" AS "type",
                T0."SalPackUn" AS "sal_pack_unit",
                T0."U_Brand" AS "brand",
                T1."WhsCode" AS "warehouse_code",
                T2."WhsName" AS "warehouse_name",
                T1."OnHand" AS "warehouse_stock",
                IFNULL(T3."RequiredQty", 0) AS "pending_required_qty",
                T1."OnHand" - IFNULL(T3."RequiredQty", 0) AS "left_over_stock",
                T1."OnHand" AS "on_hand",
                T0."OnHand" AS "total_on_hand",
                T0."validFor" AS "is_active"
            FROM "{schema}"."OITM" AS T0
            INNER JOIN "{schema}"."OITW" AS T1
                ON T0."ItemCode" = T1."ItemCode"
            INNER JOIN "{schema}"."OWHS" AS T2
                ON T1."WhsCode" = T2."WhsCode"
            INNER JOIN (
                -- Only items/warehouses that appear in an OPEN sales order, so the
                -- stock page shows just products that are in an open SO.
                SELECT
                    R1."ItemCode",
                    R1."WhsCode",
                    SUM(R1."OpenQty") AS "RequiredQty"
                FROM "{schema}"."RDR1" AS R1
                INNER JOIN "{schema}"."ORDR" AS R0
                    ON R1."DocEntry" = R0."DocEntry"
                WHERE
                    R0."CANCELED" = 'N'
                    AND R0."DocStatus" = 'O'
                    AND R1."LineStatus" = 'O'
                    AND R1."OpenQty" > 0
                GROUP BY
                    R1."ItemCode",
                    R1."WhsCode"
            ) AS T3
                ON T1."ItemCode" = T3."ItemCode"
                AND T1."WhsCode" = T3."WhsCode"
            WHERE {item_filter}
            """
            for schema, category in unique_schemas
        ]

        return "\nUNION ALL\n".join(queries)
    
    @staticmethod
    def get_party_with_open_so(branch):
        s = Queries._schema_for_branch(branch)
        branches = [
            f"""
            SELECT
                T0."CardCode",
                T0."CardName",
                COUNT(T0."DocEntry") AS "Num_of_Open_SalesOrder"
            FROM "{s}"."ORDR" T0
            WHERE T0."DocStatus" = 'O'
              AND T0."CANCELED" = 'N'
            GROUP BY T0."CardCode", T0."CardName"
            """
            # for s in Queries._open_so_schemas()
        ]
        union = "\nUNION ALL\n".join(branches)
        # Sum across company DBs so a party with open SOs in more than one DB
        # appears once with its combined total.
        return f"""
            SELECT
                "CardCode",
                "CardName",
                SUM("Num_of_Open_SalesOrder") AS "Num_of_Open_SalesOrder"
            FROM (
                {union}
            ) AS T
            GROUP BY "CardCode", "CardName"
            ORDER BY "Num_of_Open_SalesOrder" DESC
        """
    
       
    @staticmethod
    def get_quotation_status(doc_entries,  branch ,company_db=None):
        """Status of one or more Sales Quotations (OQUT) by DocEntry.

        Returns DocStatus ('O' = open, 'C' = closed) and CANCELED ('Y'/'N') so
        the caller can decide whether a quotation is still open / cancellable.

        (This text sat BELOW the schema lookup until 2026-08-27, which made it a
        bare expression rather than a docstring — `__doc__` was None.)
        """
        # An explicit company_db wins; otherwise use the branch-derived schema.
        # Interpolated, because HANA cannot bind an identifier — so it is
        # resolved through the same allow-list as every other schema name
        # rather than trusted from the caller.
        s = (Queries._schema_for_branch(company_db) if company_db
             else Queries._schema_for_branch(branch))
        entries = [int(entry) for entry in doc_entries]
        if not entries:
            return None
        # One placeholder per entry. `int()` above already makes these
        # injection-proof, but binding keeps the rule uniform: no value is ever
        # formatted into the SQL, so there is no judgement call per call site.
        placeholders = ",".join("?" for _ in entries)
        return f"""
            SELECT
                T0."DocEntry",
                T0."DocNum",
                T0."DocStatus",
                T0."CANCELED"
            FROM "{s}"."OQUT" AS T0
            WHERE T0."DocEntry" IN ({placeholders})
        """, entries

    @staticmethod
    def _scheme_from_sub_group(schema, sub_group_expr):
        """SQL for the profit-centre CODE that stands for an item sub-group.

        U_SchemeAgst holds an OPRC "PrcCode" (SUNFLOWR, GROUNDNT, RICEBRAN),
        never the sub-group name (SUNFLOWER, GROUNDNUT, RICE BRAN) -- on every
        posted invoice line it equals the line's costing code. Sub-groups are
        keyed sometimes by code and sometimes by name, so match either, in
        dimension 1 (product varieties) and active only, like
        `get_costing_code`. MIN() keeps this a scalar even if a sub-group
        matched two rows.
        """
        return (
            f'(SELECT MIN(P."PrcCode") FROM "{schema}"."OPRC" AS P'
            f' WHERE P."DimCode" = 1 AND P."Active" = \'Y\''
            f' AND (P."PrcCode" = {sub_group_expr} OR P."PrcName" = {sub_group_expr}))'
        )

    @staticmethod
    def _order_line_scheme(schema):
        """The U_SchemeAgst an invoice line drawn from this order line must carry.

        SAP's transaction validation rejects an A/R invoice line with it blank
        ("(1310325) Please Select the SchemeAgst Column"). In order: the order
        line's own value; the order line's costing code, which is already a
        PrcCode; the profit-centre code for the item's sub-group.
        """
        return (
            'COALESCE(NULLIF(T1."U_SchemeAgst", \'\'), NULLIF(T1."OcrCode", \'\'), '
            + Queries._scheme_from_sub_group(schema, 'T2."U_Sub_Group"')
            + ') AS "SchemeAgst"'
        )

    @staticmethod
    def get_sales_orders_for_party(party_code , branch):
        """Open sales-order lines for one customer, in the branch's company DB.

        "SchemeAgst" -- see `_order_line_scheme`.
        """
        s = Queries._schema_for_branch(branch)
        branches = [
            f"""
            SELECT
                T0."DocEntry", T0."DocNum", T0."DocDate", T0."DocDueDate",
                T0."CardCode", T0."CardName", T0."NumAtCard", T0."DocStatus",
                T0."DocTotal", T0."VatSum", T0."DiscSum", T0."Comments",
                T0."SlpCode", T0."ShipToCode", T0."PayToCode", T0."BPLId",
                T1."LineNum", T1."ItemCode", T1."Dscription", T1."Quantity",
                T1."OpenQty", T1."Price", T1."PriceBefDi", T1."DiscPrcnt",
                T1."LineTotal", T1."VatPrcnt", T1."VatGroup", T1."WhsCode",
                T1."TaxCode", T1."ShipDate", T1."AcctCode", T1."Project",
                T1."OcrCode", T1."LineStatus",
                {Queries._order_line_scheme(s)}
            FROM "{s}"."ORDR" AS T0
            INNER JOIN "{s}"."RDR1" AS T1
                ON T0."DocEntry" = T1."DocEntry"
            LEFT JOIN "{s}"."OITM" AS T2
                ON T2."ItemCode" = T1."ItemCode"
            WHERE T0."CardCode" = ?
              AND T0."CANCELED" = 'N'
              AND T0."DocStatus" = 'O'
              AND T1."LineStatus" = 'O'
              AND T1."OpenQty" > 0
            """
            # for s in Queries._open_so_schemas()
        ]
        # One bound value per UNION branch, in branch order. `branches` is a
        # one-element list today (the comprehension above is commented out), but
        # binding by length keeps this correct if it is restored.
        return ("\nUNION ALL\n".join(branches)
                + '\nORDER BY "DocDate" DESC, "DocNum", "LineNum"',
                [party_code] * len(branches))

    @staticmethod
    def get_sales_orders_for_product(item_code, branch=None):
        """Open sales-order lines for one item, across EVERY company database.

        `branch` is accepted and ignored, deliberately. The query UNIONs over
        `_open_so_schemas()` because open sales orders for the same item live in
        different company DBs by category, so narrowing to one branch would hide
        rows — see that helper.

        It is in the signature because `services.syncSalesOrderByProduct` has
        always passed it: the parameter was missing here, so every call raised
        `TypeError: takes 1 positional argument but 2 were given` and
        `GET /api/hana/product-so/` could never have worked. Accepting the
        argument fixes the endpoint without changing which rows come back.
        """
        branches = [
            f"""
            SELECT
                T0."DocEntry", T0."DocNum", T0."DocDate", T0."DocDueDate",
                T0."CardCode", T0."CardName", T0."NumAtCard", T0."DocStatus",
                T0."DocTotal", T0."VatSum", T0."DiscSum", T0."Comments",
                T0."SlpCode", T0."ShipToCode", T0."PayToCode", T0."BPLId",
                T1."LineNum", T1."ItemCode", T1."Dscription", T1."Quantity",
                T1."OpenQty", T1."Price", T1."PriceBefDi", T1."DiscPrcnt",
                T1."LineTotal", T1."VatPrcnt", T1."VatGroup", T1."WhsCode",
                T1."TaxCode", T1."ShipDate", T1."AcctCode", T1."Project",
                T1."OcrCode", T1."LineStatus",
                {Queries._order_line_scheme(s)}
            FROM "{s}"."ORDR" AS T0
            INNER JOIN "{s}"."RDR1" AS T1
                ON T0."DocEntry" = T1."DocEntry"
            LEFT JOIN "{s}"."OITM" AS T2
                ON T2."ItemCode" = T1."ItemCode"
            WHERE T1."ItemCode" = ?
              AND T0."CANCELED" = 'N'
              AND T0."DocStatus" = 'O'
              AND T1."LineStatus" = 'O'
              AND T1."OpenQty" > 0
            """
            for s in Queries._open_so_schemas()
        ]
        # UNION ALL over every configured company DB, so the item code binds
        # once PER BRANCH — hdbcli matches placeholders positionally, and a
        # single value would bind only the first branch.
        return ("\nUNION ALL\n".join(branches)
                + '\nORDER BY "CardName", "DocDate" DESC, "DocNum", "LineNum"',
                [item_code] * len(branches))
    
    @staticmethod
    def get_customer_details(party_code ,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
        SELECT
            T0."CardCode" ,
            T0."CardName" ,
            T0."State1" ,
            T0."U_Chain" ,
            T0."BillToDef" ,
            T0."ShipToDef" ,
            -- What the customer still owes: OCRD."Balance" is the running AR
            -- balance, already net of payments and credit memos. Positive means
            -- they owe us. Rides along here so the invoice screen needs no
            -- second round trip.
            T0."Balance"
        FROM "{s}"."OCRD" AS T0
        WHERE T0."CardCode" = ?
        """, [party_code]
        
    @staticmethod
    def get_warehouses(branch):
        """Every selectable warehouse, for the order-level warehouse picker.

        `Inactive` is left out of the filter deliberately — it is not present on
        every B1 build, and a missing column fails the whole query rather than
        degrading. `Locked` is the flag that actually blocks posting.
        """
        # MART matters here: the picker is shown on Mart orders, and falling
        # through to OIL would offer warehouses from the wrong company DB.
        if branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
            return f"""
                SELECT
                     T0."WhsCode",
                    T0."WhsName"
                FROM "{s}"."OWHS" AS T0
                WHERE T0."Locked" = 'N'
                AND T0."WhsCode" IN ('DL-MP' , 'BH-FG')
                """
        elif branch == 'MART':
            s = Queries.MART_SCHEMA
        else:
            s = Queries.OIL_SCHEMA
        return f"""
        SELECT
            T0."WhsCode",
            T0."WhsName"
        FROM "{s}"."OWHS" AS T0
        WHERE T0."Locked" = 'N'
        ORDER BY T0."WhsCode"
        """

    @staticmethod
    def get_warehouse_details(whs_code,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
        SELECT
            T0."WhsCode",
            T0."WhsName"
        FROM "{s}"."OWHS" AS T0
        WHERE T0."WhsCode" = ?
        """, [whs_code]
        
    @staticmethod
    def  get_salesperson_details(slp_code,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
        SELECT
            T0."SlpCode",
            T0."SlpName"
        FROM "{s}"."OSLP" AS T0
        WHERE T0."SlpCode" = ?
        """, [slp_code]
    @staticmethod
    def get_addresse(card_code,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
        SELECT
            T0."Address",
            T0."AdresType",
            T0."CardCode",
            T0."City",
            T0."State",
            T0."Country",
            T0."GSTRegnNo",
            T0."GSTType"
        FROM "{s}"."CRD1" AS T0
        WHERE T0."CardCode" = ?
        """, [card_code]
        
    @staticmethod
    def get_freight_masters(branch):
        s = Queries._schema_for_branch(branch)
        return f"""
            SELECT 
            	T0."ExpnsCode",
            	T0."ExpnsName"
            FROM "{s}"."OEXD" AS T0
            WHERE T0."IsActive" = 'Y'
        """  
    
    @staticmethod  
    def get_customer_state(branch):
        s = Queries._schema_for_branch(branch)
        return f"""
            SELECT 
            	DISTINCT T0."State1"
            FROM "{s}"."OCRD" AS T0
        """
        
    @staticmethod
    def get_state_chain(branch ,stateCode=None):
        s = Queries._schema_for_branch(branch)
        
        if stateCode:
            return f"""
                SELECT 
                	DISTINCT T0."U_Chain"
                FROM "{s}"."OCRD" AS T0
                    WHERE T0."State1" = ?
                """, [stateCode]
                
        return f"""
            SELECT 
            	DISTINCT T0."U_Chain"
             FROM "{s}"."OCRD" AS T0
        """
            
    @staticmethod
    def get_all_customer(branch):
        s = Queries._schema_for_branch(branch)

        return f"""
           SELECT 
                T0."U_Main_Group",
            	T0."CardCode",
            	T0."CardName",
                T0."State1",
	            T0."U_Chain",
                T0."ListNum",
                (SELECT COUNT(T1."DocEntry") FROM "{s}"."ORDR" AS T1 WHERE T1."CardCode" = T0."CardCode" AND T1."DocStatus" = 'O') AS "OpenOrders"
            FROM "{s}"."OCRD" AS T0
            LEFT JOIN "{s}"."ORDR" AS T1
            ON T1."CardCode" = T0."CardCode"
            WHERE T0."CardType" = 'C' 
            GROUP BY T0."U_Main_Group",T0."CardCode",T0."CardName",T0."State1",T0."U_Chain" , T0."ListNum"
            ORDER BY "OpenOrders" DESC
            
        """

    @staticmethod    
    def get_next_doc_no(object_code ,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
            SELECT TOP 1 T0."NextNumber"
            FROM "{s}"."NNM1" AS T0
            WHERE T0."ObjectCode" = ?
              AND T0."Locked" = 'N'
              AND T0."IsManual" = 'N'
            ORDER BY T0."Series" ASC
        """, [object_code]
        
    @staticmethod
    def get_fg_items(branch):
        s = Queries._schema_for_branch(branch)
        return f"""
        SELECT 
        	T0."ItemCode",
        	T0."ItemName",
        	T0."U_Brand",
        	T0."U_Variety",
        	T0."U_Sub_Group",
        	T0."U_SKU",
        	SUM(T1."Quantity") AS "TotalQty",
        	{Queries._scheme_from_sub_group(s, 'T0."U_Sub_Group"')} AS "SchemeAgst"
        FROM "{s}"."OITM" AS T0 
        LEFT JOIN "{s}"."OIBT" AS T1
        ON	T0."ItemCode" = T1."ItemCode"
        WHERE T0."ItemCode" LIKE 'FG%'
        GROUP BY T0."ItemCode", T0."ItemName" ,T0."U_Brand",T0."U_Variety",T0."U_Sub_Group",T0."U_SKU"
        ORDER BY "TotalQty" DESC
        """
    @staticmethod
    def get_fg_warehouse_stock(branch, item_codes=None, whs_code=None):
        """Per-warehouse on-hand stock for FG items.

        `item_codes` / `whs_code` narrow the result to the items and warehouse
        actually on an invoice — without them the whole FG catalogue across every
        warehouse comes back (a few thousand rows).
        """
        if branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        else:
            s = Queries.OIL_SCHEMA

        filters = ['T0."ItemCode" LIKE \'FG%\'']
        # Values are collected in the order their placeholders appear in the
        # FINAL string, not the order the fragments are built — hdbcli binds
        # positionally. `whs_join` lands in the JOIN, ABOVE the WHERE clause, so
        # its value binds FIRST even though the item filter is assembled first.
        # Getting this backwards would not error; it would filter by the wrong
        # column and quietly return the wrong stock.
        where_params = []
        if item_codes:
            item_codes = list(item_codes)
            filters.append(
                f'T0."ItemCode" IN ({",".join("?" for _ in item_codes)})')
            where_params.extend(item_codes)

        # Driven off OITM, not OITW: an item with no stock row for the warehouse
        # must still come back — with its name and a NULL OnHand — rather than
        # vanishing from the result.
        whs_join = ""
        join_params = []
        if whs_code:
            whs_join = 'AND T1."WhsCode" = ?'
            join_params.append(whs_code)
        where = " AND ".join(filters)

        return f"""
            SELECT
                T0."ItemCode",
                T0."ItemName",
                T1."WhsCode",
                T1."OnHand"
            FROM "{s}"."OITM" AS T0
            LEFT JOIN "{s}"."OITW" AS T1
                ON T1."ItemCode" = T0."ItemCode"
                {whs_join}
            WHERE {where}
            ORDER BY T1."OnHand" DESC, T1."WhsCode"
        """, join_params + where_params

    @staticmethod
    def get_inventory_report(branch, whs_codes=None):
        """Per-warehouse on-hand stock for every FG item, for the Inventory Report.

        One row per item/warehouse; the pivot into warehouse columns and the
        per-variety subtotals happen in `hana.utils.build_inventory_report`.
        Doing it there rather than in SQL keeps the warehouse list dynamic --
        a new warehouse in SAP shows up as a new column with no code change.

        Rows with no stock are dropped: OITW carries a row for every item in
        every warehouse (~450 items x 58 warehouses), and all but a few hundred
        of those are zeros that would only be summed back to nothing.
        """
        if branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        else:
            s = Queries.OIL_SCHEMA

        filters = [
            'T0."ItemCode" LIKE \'FG%\'',
            'T1."OnHand" <> 0',
        ]
        params = []
        if whs_codes:
            whs_codes = list(whs_codes)
            placeholders = ",".join("?" for _ in whs_codes)
            filters.append(f'T1."WhsCode" IN ({placeholders})')
            params.extend(whs_codes)
        where = " AND ".join(filters)

        return f"""
            SELECT
                T0."ItemCode"      AS "item_code",
                T0."ItemName"      AS "item_name",
                T0."U_SKU"         AS "sku",
                T0."U_Sub_Group"   AS "sub_group",
                T0."U_Variety"     AS "variety",
                T0."U_Brand"       AS "brand",
                T1."WhsCode"       AS "warehouse_code",
                T2."WhsName"       AS "warehouse_name",
                T1."OnHand"        AS "on_hand"
            FROM "{s}"."OITM" AS T0
            INNER JOIN "{s}"."OITW" AS T1
                ON T0."ItemCode" = T1."ItemCode"
            INNER JOIN "{s}"."OWHS" AS T2
                ON T1."WhsCode" = T2."WhsCode"
            WHERE {where}
            ORDER BY T0."U_Sub_Group", T0."ItemCode", T1."WhsCode"
        """, params

    @staticmethod
    def get_pending_dispatch(branch, from_date=None, to_date=None,
                             has_oms_ref=True):
        """Open sales-order lines and what has been invoiced against them.

        `has_oms_ref` says whether this company's ORDR carries the `U_OMS_REF`
        user-defined field. It does in OIL and in neither of the others, and
        naming a column that is not there fails the WHOLE query — so when it is
        absent the report selects NULL for it and still runs. The caller
        establishes this with `column_exists`; see the note there.

        The "Sales Order vs AR Invoice" report: one row per open SO line, with
        the quantity ordered, the quantity already billed, and what is still
        pending -- plus the packaging figures (case pack, litres per piece) the
        dispatch team works in.

        Invoices are matched on INV1."BaseType" = 17 (Sales Order), which is how
        this company bills: of the AR-invoice lines raised in the last three
        months, ~91% are drawn straight from a sales order and only ~1% come
        via a delivery note. A line billed through a delivery therefore shows a
        blank invoice number here, while SAP's own "OpenQty" still reports the
        pending quantity correctly -- so the pending column stays right either
        way. Cancelled invoices are excluded.
        """
        if branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        else:
            s = Queries.OIL_SCHEMA

        # NVARCHAR rather than a bare NULL so the column keeps a stable type
        # across companies and the consumers (and the Excel export) see the
        # same shape whichever branch was asked for.
        oms_ref = ('T0."U_OMS_REF"' if has_oms_ref
                   else 'CAST(NULL AS NVARCHAR(254))')

        # Every line of an open order, not just the open ones: a line already
        # billed in full closes and would otherwise vanish, leaving the order's
        # ordered/invoiced/pending totals unable to add up. Lines with nothing
        # left to send come back with a pending quantity of zero, and the
        # caller decides whether to list them.
        filters = [
            'T0."DocStatus" = \'O\'',
            'T0."CANCELED" = \'N\'',
        ]
        if from_date:
            filters.append(f'T0."DocDate" >= \'{str(from_date)[:10]}\'')
        if to_date:
            filters.append(f'T0."DocDate" <= \'{str(to_date)[:10]}\'')
        where = " AND ".join(filters)

        return f"""
            SELECT
                T0."DocEntry"        AS "so_doc_entry",
                T0."DocNum"          AS "sales_order",
                T0."DocDate"         AS "order_date",
                T0."DocDueDate"      AS "delivery_date",
                T0."CardCode"        AS "card_code",
                T0."CardName"        AS "party_name",
                T0."NumAtCard"       AS "po_number",
                {oms_ref}            AS "oms_ref",
                T0."U_OMS_Order_No"  AS "oms_order_no",
                T5."SlpName"         AS "so_name",
                T4."U_Chain"         AS "chain",
                T6."Name"            AS "location",
                T1."LineNum"         AS "line_num",
                T1."ItemCode"        AS "item_code",
                T1."Dscription"      AS "item_name",
                T1."WhsCode"         AS "warehouse_code",
                T1."Quantity"        AS "qty_ordered",
                T1."OpenQty"         AS "qty_pending",
                T1."LineStatus"      AS "line_status",
                T1."LineTotal"       AS "line_total",
                T3."U_SKU"           AS "sku",
                T3."SalFactor2"      AS "case_pack",
                T3."SalPackUn"       AS "ltr_per_pc",
                T3."U_Brand"         AS "brand",
                T3."U_Variety"       AS "oil_category",
                T3."U_Sub_Group"     AS "variety",
                T3."U_TYPE"          AS "category",
                T3."U_PACK_TYPE"     AS "case_pack_type",
                IFNULL(T2."InvQty", 0) AS "qty_invoiced",
                T2."InvNums"           AS "invoice_numbers",
                T7."InvNums"           AS "order_invoice_numbers"
            FROM "{s}"."ORDR" AS T0
            INNER JOIN "{s}"."RDR1" AS T1
                ON T0."DocEntry" = T1."DocEntry"
            LEFT JOIN (
                -- What has already been billed against each SO line. Grouped
                -- to one row per (SO line) so the join cannot multiply the
                -- order lines when a line was billed on several invoices.
                SELECT
                    I1."BaseEntry",
                    I1."BaseLine",
                    SUM(I1."Quantity") AS "InvQty",
                    STRING_AGG(TO_VARCHAR(I0."DocNum"), ', ') AS "InvNums"
                FROM "{s}"."INV1" AS I1
                INNER JOIN "{s}"."OINV" AS I0
                    ON I1."DocEntry" = I0."DocEntry"
                WHERE I1."BaseType" = 17
                  AND I0."CANCELED" = 'N'
                GROUP BY I1."BaseEntry", I1."BaseLine"
            ) AS T2
                ON T2."BaseEntry" = T1."DocEntry"
                AND T2."BaseLine" = T1."LineNum"
            LEFT JOIN (
                -- Every invoice raised against the ORDER, whichever of its
                -- lines they billed. Without this a line reads as "nothing
                -- invoiced" when its order has in fact been part-billed on a
                -- sibling line -- the dispatch desk needs to see that invoice
                -- before shipping the rest.
                -- One invoice usually bills several lines of the same order,
                -- so the DISTINCT pass runs first: HANA's STRING_AGG takes no
                -- DISTINCT of its own, and without it an invoice number would
                -- be repeated once per line it covers.
                SELECT
                    "BaseEntry",
                    STRING_AGG("InvNum", ', ') AS "InvNums"
                FROM (
                    SELECT DISTINCT
                        I1."BaseEntry",
                        TO_VARCHAR(I0."DocNum") AS "InvNum"
                    FROM "{s}"."INV1" AS I1
                    INNER JOIN "{s}"."OINV" AS I0
                        ON I1."DocEntry" = I0."DocEntry"
                    WHERE I1."BaseType" = 17
                      AND I0."CANCELED" = 'N'
                )
                GROUP BY "BaseEntry"
            ) AS T7
                ON T7."BaseEntry" = T1."DocEntry"
            LEFT JOIN "{s}"."OITM" AS T3
                ON T1."ItemCode" = T3."ItemCode"
            LEFT JOIN "{s}"."OCRD" AS T4
                ON T0."CardCode" = T4."CardCode"
            LEFT JOIN "{s}"."OSLP" AS T5
                ON T0."SlpCode" = T5."SlpCode"
            LEFT JOIN "{s}"."OCST" AS T6
                ON T4."State1" = T6."Code"
                AND T6."Country" = 'IN'
            WHERE {where}
            ORDER BY T0."DocDate", T0."DocNum", T1."LineNum"
        """

    @staticmethod
    def get_order_invoices(branch, from_date=None, to_date=None):
        """AR invoices raised against each still-open sales order.

        This is SAP's own relationship map, read straight out of the tables:
        an invoice line records the order it was drawn from in
        INV1."BaseEntry" (with "BaseType" = 17), so grouping those lines by
        invoice gives the document flow SO -> invoice(s), including how much of
        that invoice came off this order.

        Scoped by the ORDER's date, not the invoice's, so it lines up row for
        row with `get_pending_dispatch` over the same range.
        """
        if branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        else:
            s = Queries.OIL_SCHEMA

        filters = [
            'I1."BaseType" = 17',
            'I0."CANCELED" = \'N\'',
            'O."DocStatus" = \'O\'',
            'O."CANCELED" = \'N\'',
        ]
        if from_date:
            filters.append(f'O."DocDate" >= \'{str(from_date)[:10]}\'')
        if to_date:
            filters.append(f'O."DocDate" <= \'{str(to_date)[:10]}\'')
        where = " AND ".join(filters)

        return f"""
            SELECT
                I1."BaseEntry"   AS "so_doc_entry",
                I0."DocEntry"    AS "invoice_entry",
                I0."DocNum"      AS "invoice_num",
                I0."DocDate"     AS "invoice_date",
                I0."DocStatus"   AS "invoice_status",
                I0."DocTotal"    AS "invoice_total",
                SUM(I1."Quantity")  AS "qty",
                SUM(I1."LineTotal") AS "amount",
                COUNT(*)            AS "line_count"
            FROM "{s}"."INV1" AS I1
            INNER JOIN "{s}"."OINV" AS I0
                ON I1."DocEntry" = I0."DocEntry"
            INNER JOIN "{s}"."ORDR" AS O
                ON O."DocEntry" = I1."BaseEntry"
            WHERE {where}
            GROUP BY
                I1."BaseEntry", I0."DocEntry", I0."DocNum",
                I0."DocDate", I0."DocStatus", I0."DocTotal"
            ORDER BY I0."DocDate", I0."DocNum"
        """

    @staticmethod
    def get_batch_details(item_code, whs_code,branch):
        """Batches of one item in one warehouse that may actually be sold.

        Stock on hand is not the same as sellable stock. OIBT answers "how
        much is in this warehouse", but whether a batch may leave it lives in
        the batch master, OBTN."Status": 0 released, 1 not accessible,
        2 locked. Quality holds and blocked lots sit in OIBT with a positive
        quantity like anything else.

        Without the join this query offered them to the FEFO picker, which
        takes the nearest expiry and does not know the difference, and SAP
        refused the post at the very end -- "batch ... is locked or not
        accessible" (10001133), and on a second attempt the blanker "No
        matching records found" (ODBC -2028). Measured on FG0000386 in BH-SC,
        two of seven batches carried Status 2, and one of them, 160426, held
        41,890 units with no expiry date, so it sorted first on its in-date
        and swallowed every allocation the invoice asked for.

        The join is INNER on purpose: a batch row with no master is not one
        this app may pick either.
        """
        s = Queries._schema_for_branch(branch)
        return f"""
        SELECT
            T0."SysNumber",
            T0."BatchNum",
            T0."ItemCode",
            T0."ItemName",
            T0."WhsCode",
            T0."PrdDate",
            T0."ExpDate",
            T0."InDate",
            T0."Quantity",
            T0."BaseType",
            T0."BaseNum",
            T0."BaseEntry"

        FROM "{s}"."OIBT" AS T0
        INNER JOIN "{s}"."OBTN" AS T1
                ON T1."ItemCode" = T0."ItemCode"
               AND T1."SysNumber" = T0."SysNumber"
        WHERE T0."ItemCode" = ?
          AND T0."WhsCode" = ?
          AND T0."Quantity" > 0
          AND T1."Status" = '0'
        """, [item_code, whs_code]
        
    @staticmethod
    def get_inventory_details(item_code,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
           SELECT 
                DISTINCT T0."WhsCode",
                SUM(T0."Quantity")
            FROM "{s}"."OIBT" AS T0
            WHERE T0."Quantity" > 0 AND T0."ItemCode" = ?
            GROUP  BY T0."WhsCode"
        """, [item_code]
        
    @staticmethod
    def get_item_price(item_code ,  price_list,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
            SELECT 
                T0."ItemCode",
                T0."PriceList",
                T0."Price"

            FROM "{s}"."ITM1" AS T0
            WHERE T0."ItemCode" = ? AND T0."PriceList" = ?
        """, [item_code, price_list]

    @staticmethod
    def get_costing_code(prc_name, branch):
        """Resolve a Profit Center code (OPRC."PrcCode") from its name.

        SAP document lines expect the short ``CostingCode`` (PrcCode, max 8
        chars), not the human-readable profit-center name -- e.g. the variety
        "SUNFLOWER" is PrcCode "SUNFLOWR". Sending the name works only for the
        varieties where the two happen to be identical; anything longer than 8
        chars is rejected by SAP with "Value too long in property 'CostingCode'".

        Scoped to dimension 1 and active rows: PrcName is NOT unique across
        dimensions (e.g. "KARNATAKA" exists twice under DimCode 5, one of them
        inactive), so an unscoped TOP 1 can return another dimension's code --
        which SAP accepts and books to the wrong profit center. Product
        varieties always live in dimension 1.

        Looks the name up in the correct company DB schema (OIL / BEVERAGE / MART).
        """
        s = Queries._schema_for_branch(branch)
        return f"""
            SELECT TOP 1 T0."PrcCode"
            FROM "{s}"."OPRC" AS T0
            WHERE T0."PrcName" = ?
              AND T0."DimCode" = 1
              AND T0."Active" = 'Y'
        """, [prc_name]

    @staticmethod
    def get_series(finYear , BPLId,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
            SELECT 
                T0."Series",
                T0."ObjectCode",
                T0."SeriesName",
                T0."GroupCode",
                T0."Indicator"
            FROM "{s}"."NNM1" AS T0
        WHERE T0."ObjectCode" = '13' AND T0."Indicator" = ? AND T0."BPLId" = ?
        """, [finYear, BPLId]
        
    # Marketing-document tables a customer reference can already be sitting on.
    # Whitelisted because the table name is interpolated into the SQL below.
    NUM_AT_CARD_TABLES = ('OINV', 'ODRF', 'ORDR', 'OQUT')

    @staticmethod
    def get_duplicate_num_at_card(num_at_card, card_code, branch, table):
        """Find documents already holding this customer reference (NumAtCard).

        Scoped to ONE document type and ONE business partner, which is how SAP
        itself applies the rule -- the same PO legitimately flows order -> draft
        -> invoice (three rows, one ref), and distinct customers legitimately
        reuse a reference. A company-wide check would reject valid documents.

        Matched on TRIM(UPPER(...)) because SAP stores the reference uppercased
        and callers may not have normalised yet. Cancelled documents don't hold
        a reference, so they're excluded.
        """
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        else:
            raise ValueError(f"Unknown branch for NumAtCard lookup: {branch!r}")
        if table not in Queries.NUM_AT_CARD_TABLES:
            raise ValueError(f"Unsupported document table: {table!r}")

        normalised_ref = str(num_at_card).strip().upper()
        return f"""
            SELECT
                T0."DocEntry",
                T0."DocNum",
                T0."DocDate",
                T0."NumAtCard",
                T0."CardCode"
            FROM "{s}"."{table}" AS T0
            WHERE TRIM(UPPER(T0."NumAtCard")) = ?
              AND T0."CardCode" = ?
              AND T0."CANCELED" = 'N'
        """, [normalised_ref, card_code]

    @staticmethod
    def get_draft_verification(refId,branch):
        s = Queries._schema_for_branch(branch)
        return f"""SELECT * FROM "{s}"."ODRF" AS T0 WHERE T0."U_OMS_REF" = ? """, [refId]
    
    @staticmethod
    def get_invoice_status(statusCode,branch):
        s = Queries._schema_for_branch(branch)
        return f"""
        	SELECT 
		T0."WddCode",
		T0."Status",
		T0."UserSign",

		T1."DocEntry",
		T1."DocDueDate",
		T1."CardCode",
		T1."CardName",
		T1."Address",
		T1."Address2",
		T1."ShipToCode",
        T1."DocTotal",
		T1."U_OMS_REF"

		FROM "{s}"."OWDD" AS T0
		LEFT JOIN "{s}"."ODRF" AS T1
		ON T0."DraftEntry" = T1."DocEntry"
		WHERE T0."ObjType" = '13' AND T0."Status" = ? AND T1."U_OMS_REF" IS NOT NULL
        ORDER BY T1."DocDate" DESC

    """, [statusCode]
    @staticmethod
    def get_docEntry(docNum, branch='OIL'):
        """Resolve an invoice's internal key (OINV."DocEntry") from its DocNum.

        DocNum is only unique within a company database, so the branch decides
        which schema is searched (OIL / BEVERAGE / MART).
        """
        schemas = {
            'OIL': Queries.OIL_SCHEMA,
            'BEVERAGE': Queries.BEVERAGE_SCHEMA,
            'MART': Queries.MART_SCHEMA,
        }
        s = schemas.get(branch)
        if not s:
            # Better a clear error than an f-string with an undefined schema —
            # that used to raise UnboundLocalError deep in the query builder.
            raise ValueError(f'No HANA schema configured for branch {branch!r}.')
        return f"""
            SELECT
                "DocEntry"
            FROM "{s}"."OINV"
            WHERE "DocNum" = ?
        """, [docNum]

    @staticmethod
    def get_order_doc_entry(docNum, branch='OIL'):
        """Resolve a sales order's internal key (ORDR."DocEntry") from its DocNum.

        The sales-order counterpart of `get_docEntry`, and the branch matters for
        the same reason: DocNum is only unique within a company database, so the
        same number names a different order in each of the three.

        Resolves the schema through `_schema_for_branch` rather than a local dict
        of its own, so an unconfigured branch raises `HanaSchemaError` instead of
        quietly reading OIL.
        """
        s = Queries._schema_for_branch(branch)
        return f"""
            SELECT
                "DocEntry"
            FROM "{s}"."ORDR"
            WHERE "DocNum" = ?
        """, [docNum]
    



