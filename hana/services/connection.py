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