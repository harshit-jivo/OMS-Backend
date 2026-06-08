# services.py
from .connection import HANAConnection, Queries


class SalesOrderService():

    def getProductStock(self):
        with HANAConnection() as conn:
            query = Queries.get_product_stock()
            result = conn.execute(query)

        return result

    def syncSalesOrder(self, party_code):
        with HANAConnection() as conn:
            query = Queries.get_sales_orders_for_party(party_code)
            result = conn.execute(query)

        return result

    def syncSalesOrderByProduct(self, item_code):
        with HANAConnection() as conn:
            query = Queries.get_sales_orders_for_product(item_code)
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
    
    def getAddress(self, card_code):
        with HANAConnection() as conn:
            query = Queries.get_addresse(card_code)
            result = conn.execute(query)

        return result
    
    def getVendorStates(self):
        with HANAConnection() as conn:
            query = Queries().get_customer_state()
            result = conn.execute(query)

        return result
    
    def getStateChain(self, state_code=None):
        with HANAConnection() as conn:
            query = Queries.get_state_chain(state_code)
            result = conn.execute(query)

        return result

    def getAllCustomers(self):
        with HANAConnection() as conn:
            query = Queries.get_all_customer()
            result = conn.execute(query)

        return result
    
    def getNextDocNum(self, doc_type):
        with HANAConnection() as conn:
            query = Queries.get_next_doc_no(doc_type)
            result = conn.execute(query)

        return result
    
    def getFGItems(self):
        with HANAConnection() as conn:
            query = Queries.get_fg_items()
            result = conn.execute(query)

        return result
    
    def get_batch_details(self , itemCode , WhsCode):
        with HANAConnection() as conn:
            query = Queries.get_batch_details(itemCode , WhsCode)
            result = conn.execute(query)

        return result
    
    def get_inventory_details(self , itemCode):
        with HANAConnection() as conn:
            query = Queries.get_inventory_details(itemCode)
            result = conn.execute(query)

        return result
    
    def get_item_price(self , itemCode , priceList):
        with HANAConnection() as conn:
            query = Queries.get_item_price(itemCode , priceList)
            result = conn.execute(query)

        return result
