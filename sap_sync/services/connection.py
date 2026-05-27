import pymssql
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class SAPConnection:
    @staticmethod
    def _clean(value):
        if value is None:
            return value
        return str(value).strip().strip("'").strip('"')

    def __init__(self):
        self.host = self._clean(getattr(settings, 'SAP_DB_HOST', '103.89.45.75'))
        self.port = int(getattr(settings, 'SAP_DB_PORT', 1433))
        self.database = self._clean(getattr(settings, 'SAP_DB_NAME', 'Jivo_All_Branches_Live'))
        self.username = self._clean(getattr(settings, 'SAP_DB_USER', 'ab'))
        self.password = self._clean(getattr(settings, 'SAP_DB_PASSWORD', 'Jivo@!@#$'))
        self.connection = None
        self.cursor = None
    
    def connect(self):
        attempts = [
            {"server": f"{self.host},{self.port}"},
            {"server": f"{self.host}:{self.port}"},
            {"server": self.host, "port": self.port},
        ]
        last_error = None

        for params in attempts:
            try:
                self.connection = pymssql.connect(
                    user=self.username,
                    password=self.password,
                    database=self.database,
                    timeout=60,
                    login_timeout=30,
                    tds_version="7.4",
                    **params,
                )
                self.cursor = self.connection.cursor(as_dict=True)
                return True
            except Exception as e:
                last_error = e
                logger.warning(
                    "SAP DB connect attempt failed (%s): %s",
                    params,
                    str(e),
                )

        raise ConnectionError(
            f"SAP connection failed ({self.host}:{self.port}/{self.database}): {str(last_error)}"
        )
    
    def disconnect(self):
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
    
    def execute_query(self, query):
        if not self.connection:
            self.connect()
        self.cursor.execute(query)
        return self.cursor.fetchall()
    
    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
    
    @staticmethod
    def get_products_query():
        return """
            SELECT ItemCode, ItemName, Category, SalFactor2, U_Rev_tax_Rate,Deleted, U_Variety, SalPackUn, U_Brand, OnHand
            FROM OPENQUERY(HANADB112, 'SELECT "ItemCode", "ItemName", ''OIL'' AS "Category", "SalFactor2", "U_Rev_tax_Rate","Deleted", "U_Variety", "SalPackUn", "U_Brand", "OnHand"
            FROM "JIVO_OIL_HANADB"."OITM" 
            WHERE "ItemCode" LIKE ''FG%'' OR "ItemCode" LIKE ''SCH%'' OR "ItemCode" LIKE ''RM%'' OR "ItemCode" LIKE ''PM%'' OR "ItemCode" LIKE ''SC%'' ')
            UNION ALL 
            SELECT ItemCode, ItemName, Category, SalFactor2, U_Rev_tax_Rate,Deleted, U_Variety, SalPackUn, U_Brand, OnHand
            FROM OPENQUERY(HANADB112, 'SELECT "ItemCode", "ItemName", ''BEVERAGES'' AS "Category", "SalFactor2", "U_Rev_tax_Rate", "Deleted", "U_Variety", "SalPackUn", "U_Brand", "OnHand"
            FROM "JIVO_BEVERAGES_HANADB"."OITM" 
            WHERE "ItemCode" LIKE ''FG%'' OR "ItemCode" LIKE ''SCH%'' OR "ItemCode" LIKE ''RM%'' OR "ItemCode" LIKE ''PM%'' OR "ItemCode" LIKE ''SC%'' ')
            UNION ALL
            SELECT ItemCode, ItemName, Category, SalFactor2, U_Rev_tax_Rate,Deleted, U_Variety, SalPackUn, U_Brand, OnHand
            FROM OPENQUERY(HANADB112, 'SELECT "ItemCode", "ItemName", ''MART'' AS "Category", "SalFactor2", "U_Rev_tax_Rate", "Deleted", "U_Variety", "SalPackUn", "U_Brand", "OnHand" 
            FROM "JIVO_MART_HANADB"."OITM" 
            WHERE "ItemCode" LIKE ''FG%'' OR "ItemCode" LIKE ''SCH%'' OR "ItemCode" LIKE ''RM%'' OR "ItemCode" LIKE ''PM%'' OR "ItemCode" LIKE ''SC%'' ')
        """

    @staticmethod
    def get_live_stock_query(category, item_codes):
        db_by_category = {
            "OIL": "JIVO_OIL_HANADB",
            "BEVERAGES": "JIVO_BEVERAGES_HANADB",
            "MART": "JIVO_MART_HANADB",
        }
        normalized_category = str(category or "").strip().upper()
        database = db_by_category.get(normalized_category)
        cleaned_item_codes = [
            str(item_code or "").strip()
            for item_code in item_codes
            if str(item_code or "").strip()
        ]

        if not database or not cleaned_item_codes:
            return None

        quoted_item_codes = ", ".join(
            "''{}''".format(item_code.replace("'", "''"))
            for item_code in sorted(set(cleaned_item_codes))
        )

        return f"""
            SELECT ItemCode, Category, OnHand
            FROM OPENQUERY(HANADB112, 'SELECT "ItemCode", ''{normalized_category}'' AS "Category", "OnHand"
            FROM "{database}"."OITM"
            WHERE "ItemCode" IN ({quoted_item_codes})')
        """
    
    @staticmethod
    def get_parties_query():
        return """
            SELECT CardCode, CardName, Address, State1, U_Main_Group, U_Chain, Country, CardType, LicTradNum, Category
            FROM OPENQUERY(HANADB112, '
                SELECT 
                    "CardCode", 
                    "CardName", 
                    "Address", 
                    "State1", 
                    "U_Main_Group", 
                    "U_Chain", 
                    "Country", 
                    "CardType", 
                    "LicTradNum",
                    ''OIL'' AS "Category"
                FROM "JIVO_OIL_HANADB"."OCRD" 
                WHERE "CardType"=''C''
            ')
            UNION ALL
            SELECT CardCode, CardName, Address, State1, U_Main_Group, U_Chain, Country, CardType, LicTradNum, Category
            FROM OPENQUERY(HANADB112, '
                SELECT 
                    "CardCode", 
                    "CardName", 
                    "Address", 
                    "State1", 
                    "U_Main_Group", 
                    "U_Chain", 
                    "Country", 
                    "CardType", 
                    "LicTradNum",
                    ''BEVERAGES'' AS "Category"
                FROM "JIVO_BEVERAGES_HANADB"."OCRD" 
                WHERE "CardType"=''C''
            ')
            UNION ALL
            SELECT CardCode, CardName, Address, State1, U_Main_Group, U_Chain, Country, CardType, LicTradNum, Category
            FROM OPENQUERY(HANADB112, '
                SELECT 
                    "CardCode", 
                    "CardName", 
                    "Address", 
                    "State1", 
                    "U_Main_Group", 
                    "U_Chain", 
                    "Country", 
                    "CardType", 
                    "LicTradNum",
                    ''MART'' AS "Category"
                FROM "JIVO_MART_HANADB"."OCRD" 
                WHERE "CardType"=''C''
            ')
        """

    @staticmethod
    def get_party_addresses_query():
        return """
            SELECT CardCode, Address, AdresType, GSTRegnNo, State, City, ZipCode, Country, Category,
            CONCAT(ISNULL(Address2,''), ' ', ISNULL(Address3,''), ' ', ISNULL(Street,''), ' ', ISNULL(Block,''), ' ', ISNULL(City,''), ' ', ISNULL(State,''), ' ', ISNULL(Country,''), ' ', ISNULL(ZipCode,'')) AS MainAddress 
            FROM OPENQUERY(HANADB112, '
                SELECT 
                    "CardCode", 
                    "Address", 
                    "AdresType", 
                    "GSTRegnNo", 
                    "State", 
                    "City", 
                    "ZipCode", 
                    "Country", 
                    "Address2", 
                    "Address3", 
                    "Street", 
                    "Block",
                    ''OIL'' AS "Category"
                FROM "JIVO_OIL_HANADB"."CRD1"
            ')
            UNION ALL
            SELECT CardCode, Address, AdresType, GSTRegnNo, State, City, ZipCode, Country, Category,
            CONCAT(ISNULL(Address2,''), ' ', ISNULL(Address3,''), ' ', ISNULL(Street,''), ' ', ISNULL(Block,''), ' ', ISNULL(City,''), ' ', ISNULL(State,''), ' ', ISNULL(Country,''), ' ', ISNULL(ZipCode,'')) AS MainAddress 
            FROM OPENQUERY(HANADB112, '
                SELECT 
                    "CardCode", 
                    "Address", 
                    "AdresType", 
                    "GSTRegnNo", 
                    "State", 
                    "City", 
                    "ZipCode", 
                    "Country", 
                    "Address2", 
                    "Address3", 
                    "Street", 
                    "Block",
                    ''BEVERAGES'' AS "Category"
                FROM "JIVO_BEVERAGES_HANADB"."CRD1"
            ')
            UNION ALL
            SELECT CardCode, Address, AdresType, GSTRegnNo, State, City, ZipCode, Country, Category,
            CONCAT(ISNULL(Address2,''), ' ', ISNULL(Address3,''), ' ', ISNULL(Street,''), ' ', ISNULL(Block,''), ' ', ISNULL(City,''), ' ', ISNULL(State,''), ' ', ISNULL(Country,''), ' ', ISNULL(ZipCode,'')) AS MainAddress 
            FROM OPENQUERY(HANADB112, '
                SELECT 
                    "CardCode", 
                    "Address", 
                    "AdresType", 
                    "GSTRegnNo", 
                    "State", 
                    "City", 
                    "ZipCode", 
                    "Country", 
                    "Address2", 
                    "Address3", 
                    "Street", 
                    "Block",
                    ''MART'' AS "Category"
                FROM "JIVO_MART_HANADB"."CRD1"
            ')
        """

    @staticmethod
    def get_branches_query():
        """Get branches from OBPL table across all 3 databases"""
        return """
            SELECT BPLId, BPLName, Category
            FROM OPENQUERY(HANADB112, 'SELECT "BPLId", "BPLName", ''OIL'' AS "Category" FROM "JIVO_OIL_HANADB"."OBPL"')
            UNION ALL
            SELECT BPLId, BPLName, Category
            FROM OPENQUERY(HANADB112, 'SELECT "BPLId", "BPLName", ''BEVERAGES'' AS "Category" FROM "JIVO_BEVERAGES_HANADB"."OBPL"')
            UNION ALL
            SELECT BPLId, BPLName, Category
            FROM OPENQUERY(HANADB112, 'SELECT "BPLId", "BPLName", ''MART'' AS "Category" FROM "JIVO_MART_HANADB"."OBPL"')
        """
