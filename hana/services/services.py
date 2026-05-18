# services.py
from .connection import HANAConnection, Queries


class SalesOrderService():

    def syncSalesOrder(self, party_code):
        with HANAConnection() as conn:
            query = Queries.get_sales_orders_for_party(party_code)
            result = conn.execute(query)

        return result