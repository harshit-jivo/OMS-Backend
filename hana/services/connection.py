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
    def get_sales_orders():
        s = Queries.SCHEMA
        return f"""
            SELECT
                T0."DocNum",
                T0."DocDate",
                T0."CardCode",
                T0."CardName",
                T0."DocTotal",
                T0."DocStatus"
            FROM "{s}"."ORDR" T0
            ORDER BY T0."DocDate" DESC
        """