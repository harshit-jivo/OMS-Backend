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
            return True
              
        except Exception as e:
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
    SCHEMA = settings.DATABASES['hana']['SCHEMA']
    
    @staticmethod
    def get_party_with_open_so():
        s = Queries.SCHEMA
        return f"""
            SELECT
                T0."CardCode",
                T0."CardName",
                COUNT(T0."DocEntry") AS "Num_of_Open_SalesOrder"
            FROM "{s}"."ORDR" T0
            WHERE T0."DocStatus" = 'O'
              AND T0."CANCELED" = 'N'
            GROUP BY T0."CardCode", T0."CardName"
            ORDER BY "Num_of_Open_SalesOrder" DESC
        """
    
       
    @staticmethod
    def get_sales_orders_for_party(party_code):
        s = Queries.SCHEMA
        return f"""
        SELECT
            T0."DocEntry",
            T0."DocNum",
            T0."DocDate",
            T0."DocDueDate",
            T0."CardCode",
            T0."CardName",
            T0."NumAtCard",
            T0."DocStatus",
            T0."DocTotal",
            T0."VatSum",
            T0."DiscSum",
            T0."Comments",
            T0."SlpCode",
            T0."ShipToCode",
            T0."PayToCode",
            T0."BPLId",

            T1."LineNum",
            T1."ItemCode",
            T1."Dscription",
            T1."Quantity",
            T1."OpenQty",
            T1."Price",
            T1."PriceBefDi",
            T1."DiscPrcnt",
            T1."LineTotal",
            T1."VatPrcnt",
            T1."VatGroup",
            T1."WhsCode",
            T1."TaxCode",
            T1."ShipDate",
            T1."AcctCode",
            T1."Project",
            T1."OcrCode",
            T1."LineStatus"

        FROM "{s}"."ORDR" AS T0
        INNER JOIN "{s}"."RDR1" AS T1
            ON T0."DocEntry" = T1."DocEntry"

        WHERE T0."CardCode" = '{party_code}'
          AND T0."DocStatus" = 'O'
          AND T1."LineStatus" = 'O'
          AND T1."OpenQty" > 0

        ORDER BY T0."DocDate" DESC, T0."DocNum", T1."LineNum"
        """ 
    
    @staticmethod
    def get_customer_details(party_code):
        s = Queries.SCHEMA
        return f"""
        SELECT
            T0."CardCode" , 
            T0."CardName" , 
            T0."State1" ,  
            T0."U_Chain" ,  
            T0."BillToDef" , 
            T0."ShipToDef" 	 
        FROM "{s}"."OCRD" AS T0
        WHERE T0."CardCode" = '{party_code}'
        """
        
    @staticmethod
    def get_warehouse_details(whs_code):
        s = Queries.SCHEMA
        return f"""
        SELECT
            T0."WhsCode",
            T0."WhsName"
        FROM "{s}"."OWHS" AS T0
        WHERE T0."WhsCode" = '{whs_code}'
        """ 
        
    @staticmethod
    def  get_salesperson_details(slp_code):
        s = Queries.SCHEMA
        return f"""
        SELECT
            T0."SlpCode",
            T0."SlpName"
        FROM "{s}"."OSLP" AS T0
        WHERE T0."SlpCode" = '{slp_code}'
        """
    @staticmethod
    def get_addresse(card_code):
        s = Queries.SCHEMA
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
    def get_freight_masters():
        s = Queries.SCHEMA
        return f"""
            SELECT 
            	T0."ExpnsCode",
            	T0."ExpnsName"
            FROM "{s}"."OEXD" AS T0
            WHERE T0."IsActive" = 'Y'
        """  
    
    @staticmethod  
    def get_customer_state():
        s = Queries.SCHEMA
        return f"""
            SELECT 
            	DISTINCT T0."State1"
            FROM "{s}"."OCRD" AS T0
        """
        
    @staticmethod
    def get_state_chain(stateCode=None):
        s = Queries.SCHEMA
        
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
    def get_all_customer():
        s = Queries.SCHEMA
        return f"""
            SELECT 
            	T0."CardCode",
            	T0."CardName",
                T0."State1",
	            T0."U_Chain"
            FROM "{s}"."OCRD" AS T0
            WHERE T0."CardType" = 'C'
        """

    @staticmethod    
    def get_next_doc_no(object_code):
        s = Queries.SCHEMA
        return f"""
            SELECT TOP 1 T0."NextNumber"
            FROM "{s}"."NNM1" AS T0
            WHERE T0."ObjectCode" = '{object_code}'
              AND T0."Locked" = 'N'
              AND T0."IsManual" = 'N'
            ORDER BY T0."Series" ASC
        """
        
    @staticmethod
    def get_fg_items():
        s = Queries.SCHEMA 
        return f"""
        SELECT 
            T0."ItemCode",  
            T0."ItemName",
            T0."U_Brand",
            T0."U_Variety",
	        T0."U_Sub_Group",
	        T0."U_SKU"
         
        FROM "{s}"."OITM" AS T0
        WHERE T0."ItemCode" LIKE 'FG%'
     
        """
    @staticmethod
    def get_batch_details(item_code, whs_code):
        s = Queries.SCHEMA
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
    def get_inventory_details(item_code):
        s = Queries.SCHEMA
        return f"""
           SELECT 
                DISTINCT T0."WhsCode",
                SUM(T0."Quantity")
            FROM "JIVO_OIL_HANADB"."OIBT" AS T0
            WHERE T0."Quantity" > 0 AND T0."ItemCode" = '{item_code}'
            GROUP  BY T0."WhsCode"
        """
        