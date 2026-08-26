from hdbcli import dbapi
from django.conf import settings

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
            print("Connected to HANA successfully")
            return True
              
        except Exception as e:
            print(e)
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
        
    def execute(self, sql: str, params=None):
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



class Queries():
    OIL_SCHEMA = settings.DATABASES['hana']['OIL_SCHEMA']
    BEVERAGE_SCHEMA = settings.DATABASES['hana']['BEVERAGE_SCHEMA']
    # Third company. Only the DocNum -> DocEntry lookup (bill print) is wired for
    # it so far; the other queries below are still OIL/BEVERAGE only.
    MART_SCHEMA = getattr(settings, 'HANA_MART_COMPANY_DB', '')

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
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
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
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        """Status of one or more Sales Quotations (OQUT) by DocEntry.

        Returns DocStatus ('O' = open, 'C' = closed) and CANCELED ('Y'/'N') so
        the caller can decide whether a quotation is still open / cancellable.
        """
        # An explicit company_db wins; otherwise use the branch-derived schema above.
        s = str(company_db or s).strip().replace('"', '""')
        safe_entries = [str(int(entry)) for entry in doc_entries]
        if not safe_entries:
            return None
        entries_csv = ",".join(safe_entries)
        return f"""
            SELECT
                T0."DocEntry",
                T0."DocNum",
                T0."DocStatus",
                T0."CANCELED"
            FROM "{s}"."OQUT" AS T0
            WHERE T0."DocEntry" IN ({entries_csv})
        """

    @staticmethod
    def get_sales_orders_for_party(party_code , branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
            
        safe_party_code = str(party_code).replace("'", "''")
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
                T1."OcrCode", T1."LineStatus"
            FROM "{s}"."ORDR" AS T0
            INNER JOIN "{s}"."RDR1" AS T1
                ON T0."DocEntry" = T1."DocEntry"
            WHERE T0."CardCode" = '{safe_party_code}'
              AND T0."CANCELED" = 'N'
              AND T0."DocStatus" = 'O'
              AND T1."LineStatus" = 'O'
              AND T1."OpenQty" > 0
            """
            # for s in Queries._open_so_schemas()
        ]
        return "\nUNION ALL\n".join(branches) + '\nORDER BY "DocDate" DESC, "DocNum", "LineNum"'

    @staticmethod
    def get_sales_orders_for_product(item_code):
        safe_item_code = str(item_code).replace("'", "''")

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
                T1."OcrCode", T1."LineStatus"
            FROM "{s}"."ORDR" AS T0
            INNER JOIN "{s}"."RDR1" AS T1
                ON T0."DocEntry" = T1."DocEntry"
            WHERE T1."ItemCode" = '{safe_item_code}'
              AND T0."CANCELED" = 'N'
              AND T0."DocStatus" = 'O'
              AND T1."LineStatus" = 'O'
              AND T1."OpenQty" > 0
            """
            for s in Queries._open_so_schemas()
        ]
        return "\nUNION ALL\n".join(branches) + '\nORDER BY "CardName", "DocDate" DESC, "DocNum", "LineNum"'
    
    @staticmethod
    def get_customer_details(party_code ,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
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
        WHERE T0."CardCode" = '{party_code}'
        """
        
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
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
        SELECT
            T0."WhsCode",
            T0."WhsName"
        FROM "{s}"."OWHS" AS T0
        WHERE T0."WhsCode" = '{whs_code}'
        """ 
        
    @staticmethod
    def  get_salesperson_details(slp_code,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
        SELECT
            T0."SlpCode",
            T0."SlpName"
        FROM "{s}"."OSLP" AS T0
        WHERE T0."SlpCode" = '{slp_code}'
        """
    @staticmethod
    def get_addresse(card_code,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
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
        WHERE T0."CardCode" = '{card_code}'
        """
        
    @staticmethod
    def get_freight_masters(branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
            SELECT 
            	T0."ExpnsCode",
            	T0."ExpnsName"
            FROM "{s}"."OEXD" AS T0
            WHERE T0."IsActive" = 'Y'
        """  
    
    @staticmethod  
    def get_customer_state(branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
            SELECT 
            	DISTINCT T0."State1"
            FROM "{s}"."OCRD" AS T0
        """
        
    @staticmethod
    def get_state_chain(branch ,stateCode=None):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        
        if stateCode:
            return f"""
                SELECT 
                	DISTINCT T0."U_Chain"
                FROM "{s}"."OCRD" AS T0
                    WHERE T0."State1" = '{stateCode}'
                """
                
        return f"""
            SELECT 
            	DISTINCT T0."U_Chain"
             FROM "{s}"."OCRD" AS T0
        """
            
    @staticmethod
    def get_all_customer(branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA

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
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
            SELECT TOP 1 T0."NextNumber"
            FROM "{s}"."NNM1" AS T0
            WHERE T0."ObjectCode" = '{object_code}'
              AND T0."Locked" = 'N'
              AND T0."IsManual" = 'N'
            ORDER BY T0."Series" ASC
        """
        
    @staticmethod
    def get_fg_items(branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA 
        return f"""
        SELECT 
        	T0."ItemCode",
        	T0."ItemName",
        	T0."U_Brand",
        	T0."U_Variety",
        	T0."U_Sub_Group",
        	T0."U_SKU",
        	SUM(T1."Quantity") AS "TotalQty"
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
        if item_codes:
            safe_codes = ",".join(
                "'" + str(code).replace("'", "''") + "'" for code in item_codes
            )
            filters.append(f'T0."ItemCode" IN ({safe_codes})')

        # Driven off OITM, not OITW: an item with no stock row for the warehouse
        # must still come back — with its name and a NULL OnHand — rather than
        # vanishing from the result.
        whs_join = ""
        if whs_code:
            safe_whs = str(whs_code).replace("'", "''")
            whs_join = f"""AND T1."WhsCode" = '{safe_whs}'"""
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
        """

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
        if whs_codes:
            safe_codes = ",".join(
                "'" + str(code).replace("'", "''") + "'" for code in whs_codes
            )
            filters.append(f'T1."WhsCode" IN ({safe_codes})')
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
        """

    @staticmethod
    def get_pending_dispatch(branch, from_date=None, to_date=None):
        """Open sales-order lines and what has been invoiced against them.

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
                T0."U_OMS_REF"       AS "oms_ref",
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
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
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
        WHERE T0."ItemCode" = '{item_code}'
          AND T0."WhsCode" = '{whs_code}'
          AND T0."Quantity" > 0
        """
        
    @staticmethod
    def get_inventory_details(item_code,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
           SELECT 
                DISTINCT T0."WhsCode",
                SUM(T0."Quantity")
            FROM "{s}"."OIBT" AS T0
            WHERE T0."Quantity" > 0 AND T0."ItemCode" = '{item_code}'
            GROUP  BY T0."WhsCode"
        """
        
    @staticmethod
    def get_item_price(item_code ,  price_list,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
            SELECT 
                T0."ItemCode",
                T0."PriceList",
                T0."Price"

            FROM "{s}"."ITM1" AS T0
            WHERE T0."ItemCode" = '{item_code}' AND T0."PriceList" = {price_list}
        """

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
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        elif branch == 'MART':
            s = Queries.MART_SCHEMA
        safe_prc_name = str(prc_name).replace("'", "''")
        return f"""
            SELECT TOP 1 T0."PrcCode"
            FROM "{s}"."OPRC" AS T0
            WHERE T0."PrcName" = '{safe_prc_name}'
              AND T0."DimCode" = 1
              AND T0."Active" = 'Y'
        """

    @staticmethod
    def get_series(finYear , BPLId,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""
            SELECT 
                T0."Series",
                T0."ObjectCode",
                T0."SeriesName",
                T0."GroupCode",
                T0."Indicator"
            FROM "{s}"."NNM1" AS T0
        WHERE T0."ObjectCode" = '13' AND T0."Indicator" = '{finYear }' AND T0."BPLId" = '{BPLId}'
        """
        
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

        safe_ref = str(num_at_card).strip().upper().replace("'", "''")
        safe_card = str(card_code).replace("'", "''")
        return f"""
            SELECT
                T0."DocEntry",
                T0."DocNum",
                T0."DocDate",
                T0."NumAtCard",
                T0."CardCode"
            FROM "{s}"."{table}" AS T0
            WHERE TRIM(UPPER(T0."NumAtCard")) = '{safe_ref}'
              AND T0."CardCode" = '{safe_card}'
              AND T0."CANCELED" = 'N'
        """

    @staticmethod
    def get_draft_verification(refId,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
        return f"""SELECT * FROM "{s}"."ODRF" AS T0 WHERE T0."U_OMS_REF" = '{refId}' """
    
    @staticmethod
    def get_invoice_status(statusCode,branch):
        if branch == 'OIL':
            s = Queries.OIL_SCHEMA
        elif branch == 'BEVERAGE':
            s = Queries.BEVERAGE_SCHEMA
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
		WHERE T0."ObjType" = '13' AND T0."Status" = '{statusCode}' AND T1."U_OMS_REF" IS NOT NULL
        ORDER BY T1."DocDate" DESC

    """
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
            WHERE "DocNum" = '{docNum}'
        """
    



