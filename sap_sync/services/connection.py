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

    @staticmethod
    def _schemas():
        """(oil, beverage, mart) company-DB schema names, sourced from settings
        (which reads them from .env). No schema name is hardcoded in this module;
        the SQL builders below interpolate these."""
        return (
            settings.HANA_OIL_COMPANY_DB,
            settings.HANA_BEVERAGE_COMPANY_DB,
            settings.HANA_MART_COMPANY_DB,
        )

    def __init__(self):
        # Read straight off settings, with no `getattr` fallback. Every one of
        # these used to carry the production host, database, user and password
        # as a literal default here AS WELL AS in settings.py — so removing them
        # from settings alone would have changed nothing, and clearing the .env
        # would have silently reconnected to production instead of failing.
        #
        # settings.py declares all five without a default, so they are always
        # present; an unset one stops the process at startup with the key named.
        self.host = self._clean(settings.SAP_DB_HOST)
        self.port = int(settings.SAP_DB_PORT)
        self.database = self._clean(settings.SAP_DB_NAME)
        self.username = self._clean(settings.SAP_DB_USER)
        self.password = self._clean(settings.SAP_DB_PASSWORD)
        self.connection = None
        self.cursor = None
    
    def connect(self):
        # This host only answers to the FreeTDS host:port / separate-port forms;
        # the "host,port" form is kept last so a move to a named instance or a
        # different FreeTDS build keeps working.
        attempts = [
            {"server": self.host, "port": self.port},
            {"server": f"{self.host}:{self.port}"},
            {"server": f"{self.host},{self.port}"},
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
                # Per-attempt failures are expected while walking the ladder;
                # the raised ConnectionError below is the real signal.
                logger.debug(
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
        oil, bev, mart = SAPConnection._schemas()
        return f"""
            SELECT ItemCode, ItemName, Category, SalFactor2, U_Rev_tax_Rate,Deleted, U_Variety, U_Sub_Group, SalPackUn, U_Brand, OnHand, U_TYPE,validFor
            FROM OPENQUERY(HANADB112, 'SELECT "ItemCode", "ItemName", ''OIL'' AS "Category", "SalFactor2", "U_Rev_tax_Rate","Deleted", "U_Variety", "U_Sub_Group","SalPackUn", "U_Brand", "OnHand", "U_TYPE", "validFor"
            FROM "{oil}"."OITM"
            WHERE "ItemCode" LIKE ''FG%'' OR "ItemCode" LIKE ''SCH%'' OR "ItemCode" LIKE ''RM%'' OR "ItemCode" LIKE ''PM%'' OR "ItemCode" LIKE ''SC%'' OR "ItemCode" LIKE ''CG%''  ')
            UNION ALL
            SELECT ItemCode, ItemName, Category, SalFactor2, U_Rev_tax_Rate,Deleted, U_Variety, U_Sub_Group, SalPackUn, U_Brand, OnHand, U_TYPE,    validFor
            FROM OPENQUERY(HANADB112, 'SELECT "ItemCode", "ItemName", ''BEVERAGES'' AS "Category", "SalFactor2", "U_Rev_tax_Rate", "Deleted", "U_Variety", "U_Sub_Group","SalPackUn", "U_Brand", "OnHand", "U_TYPE", "validFor"
            FROM "{bev}"."OITM"
            WHERE "ItemCode" LIKE ''FG%'' OR "ItemCode" LIKE ''SCH%'' OR "ItemCode" LIKE ''RM%'' OR "ItemCode" LIKE ''PM%'' OR "ItemCode" LIKE ''SC%'' OR "ItemCode" LIKE ''CG%'' ')
            UNION ALL
            SELECT ItemCode, ItemName, Category, SalFactor2, U_Rev_tax_Rate,Deleted, U_Variety, U_Sub_Group, SalPackUn, U_Brand, OnHand, U_TYPE, validFor
            FROM OPENQUERY(HANADB112, 'SELECT "ItemCode", "ItemName", ''MART'' AS "Category", "SalFactor2", "U_Rev_tax_Rate", "Deleted", "U_Variety", "U_Sub_Group","SalPackUn", "U_Brand", "OnHand", "U_TYPE", "validFor"
            FROM "{mart}"."OITM"
            WHERE "ItemCode" LIKE ''FG%'' OR "ItemCode" LIKE ''SCH%'' OR "ItemCode" LIKE ''RM%'' OR "ItemCode" LIKE ''PM%'' OR "ItemCode" LIKE ''SC%'' OR "ItemCode" LIKE ''CG%''')
        """

    @staticmethod
    def get_live_stock_query(category, item_codes):
        oil, bev, mart = SAPConnection._schemas()
        db_by_category = {
            "OIL": oil,
            "BEVERAGES": bev,
            "MART": mart,
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
        oil, bev, mart = SAPConnection._schemas()
        return f"""
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
                FROM "{oil}"."OCRD"
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
                FROM "{bev}"."OCRD"
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
                FROM "{mart}"."OCRD"
                WHERE "CardType"=''C''
            ')
        """

    @staticmethod
    def get_party_addresses_query():
        oil, bev, mart = SAPConnection._schemas()
        return f"""
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
                FROM "{oil}"."CRD1"
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
                FROM "{bev}"."CRD1"
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
                FROM "{mart}"."CRD1"
            ')
        """

    @staticmethod
    def get_branches_query():
        """Get branches from OBPL table across all 3 databases"""
        oil, bev, mart = SAPConnection._schemas()
        return f"""
            SELECT BPLId, BPLName, Category
            FROM OPENQUERY(HANADB112, 'SELECT "BPLId", "BPLName", ''OIL'' AS "Category" FROM "{oil}"."OBPL"')
            UNION ALL
            SELECT BPLId, BPLName, Category
            FROM OPENQUERY(HANADB112, 'SELECT "BPLId", "BPLName", ''BEVERAGES'' AS "Category" FROM "{bev}"."OBPL"')
            UNION ALL
            SELECT BPLId, BPLName, Category
            FROM OPENQUERY(HANADB112, 'SELECT "BPLId", "BPLName", ''MART'' AS "Category" FROM "{mart}"."OBPL"')
        """
