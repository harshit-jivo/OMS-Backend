# services.py
from .connection import HANAConnection, Queries


class SalesOrderService():

    def syncSalesOrder(self, party_code):
        with HANAConnection() as conn:
            query = Queries.get_sales_orders_for_party(party_code)
            result = conn.execute(query)

        return result
    
    def syncOpenParties(self):
        with HANAConnection() as conn:
            query = Queries.get_party_with_open_so()
            result = conn.execute(query)

        return result
    
    def getCustomerDetails(self, party_code):
        with HANAConnection() as conn:
            query = Queries.get_customer_details(party_code)
            result = conn.execute(query)

        return result
    
    def getWarehouseDetails(self, warehouse_code):
        with HANAConnection() as conn:
            query = Queries.get_warehouse_details(warehouse_code)
            result = conn.execute(query)

        return result
    
    def getSalespersonDetails(self, salesperson_code):
        with HANAConnection() as conn:
            query = Queries.get_salesperson_details(salesperson_code)
            result = conn.execute(query)

        return result
    
    def FreightMasters(self):
        with HANAConnection() as conn:
            query = Queries.get_freight_masters()
            result = conn.execute(query)

        return result