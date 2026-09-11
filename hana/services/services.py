# services.py
from .connection import HANAConnection, Queries, column_exists


class SalesOrderService():

    def getProductStock(self,branch):
        with HANAConnection() as conn:
            query = Queries.get_product_stock(branch)
            result = conn.execute(query)

        return result

    def getInventoryReport(self, branch, whs_codes=None):
        with HANAConnection() as conn:
            query = Queries.get_inventory_report(branch, whs_codes)
            result = conn.execute(query)

        return result

    def getPendingDispatch(self, branch, from_date=None, to_date=None):
        """Open sales-order lines and the invoices raised against their orders.

        Both queries ride on one HANA connection: they are two halves of the
        same report, and opening a second connection for the invoice half would
        double the connect cost for no gain.
        """
        with HANAConnection() as conn:
            # `U_OMS_REF` is a user-defined field on the OIL company's ORDR and
            # on no other, so naming it unconditionally failed the entire
            # beverage report with "invalid column name". Asked once per
            # process per company; see `column_exists`.
            has_oms_ref = column_exists(
                conn, Queries._schema_for_branch(branch), 'ORDR', 'U_OMS_REF')
            lines = conn.execute(
                Queries.get_pending_dispatch(
                    branch, from_date, to_date, has_oms_ref)
            )
            invoices = conn.execute(
                Queries.get_order_invoices(branch, from_date, to_date)
            )

        return lines, invoices

    def syncSalesOrder(self, party_code,branch):
        with HANAConnection() as conn:
            query = Queries.get_sales_orders_for_party(party_code,branch)
            result = conn.execute(query)

        return result

    def syncSalesOrderByProduct(self, item_code,branch):
        with HANAConnection() as conn:
            query = Queries.get_sales_orders_for_product(item_code,branch)
            result = conn.execute(query)

        return result
    
    def syncOpenParties(self,branch):
        with HANAConnection() as conn:
            query = Queries.get_party_with_open_so(branch)
            result = conn.execute(query)

        return result
    
    def getCustomerDetails(self, party_code,branch):
        with HANAConnection() as conn:
            query = Queries.get_customer_details(party_code,branch)
            result = conn.execute(query)

        return result
    
    def getWarehouses(self, branch):
        with HANAConnection() as conn:
            query = Queries.get_warehouses(branch)
            result = conn.execute(query)

        return result

    def getWarehouseDetails(self, warehouse_code,branch):
        with HANAConnection() as conn:
            query = Queries.get_warehouse_details(warehouse_code,branch)
            result = conn.execute(query)

        return result
    
    def getSalespersonDetails(self, salesperson_code,branch):
        with HANAConnection() as conn:
            query = Queries.get_salesperson_details(salesperson_code,branch)
            result = conn.execute(query)

        return result
    
    def FreightMasters(self,branch):
        with HANAConnection() as conn:
            query = Queries.get_freight_masters(branch)
            result = conn.execute(query)

        return result
    
    def getAddress(self, card_code,branch):
        with HANAConnection() as conn:
            query = Queries.get_addresse(card_code,branch)
            result = conn.execute(query)

        return result
    
    def getVendorStates(self,branch):
        with HANAConnection() as conn:
            query = Queries().get_customer_state(branch)
            result = conn.execute(query)

        return result
    
    def getStateChain(self, branch ,state_code=None):
        with HANAConnection() as conn:
            query = Queries.get_state_chain(branch ,state_code)
            result = conn.execute(query)

        return result

    def getAllCustomers(self , branch):
        with HANAConnection() as conn:
            query = Queries.get_all_customer(branch)
            result = conn.execute(query)

        return result
    
    def getNextDocNum(self, doc_type, branch):
        with HANAConnection() as conn:
            query = Queries.get_next_doc_no(doc_type, branch)
            result = conn.execute(query)

        return result
    
    def getFGItems(self, branch):
        with HANAConnection() as conn:
            query = Queries.get_fg_items(branch)
            result = conn.execute(query)

        return result
    
    def get_fg_warehouse_stock(self, branch, item_codes=None, whs_code=None):
        with HANAConnection() as conn:
            query = Queries.get_fg_warehouse_stock(branch, item_codes, whs_code)
            result = conn.execute(query)

        return result

    def get_batch_details(self , itemCode , WhsCode, branch):
        with HANAConnection() as conn:
            query = Queries.get_batch_details(itemCode , WhsCode, branch)
            result = conn.execute(query)

        return result
    
    def get_inventory_details(self , itemCode, branch):
        with HANAConnection() as conn:
            query = Queries.get_inventory_details(itemCode, branch)
            result = conn.execute(query)

        return result
    
    def get_item_price(self , itemCode , priceList, branch):
        with HANAConnection() as conn:
            query = Queries.get_item_price(itemCode , priceList, branch)
            result = conn.execute(query)

        return result

    def get_quotation_status(self, doc_entries,  branch ,company_db=None):
        query = Queries.get_quotation_status(doc_entries, branch ,company_db=company_db)
        if not query:
            return []
        with HANAConnection() as conn:
            result = conn.execute(query)

        return result

    def get_costing_code(self, prc_name, branch):
        """Return the Profit Center code (PrcCode) for a profit-center name.

        Returns the scalar PrcCode, or None if the name is blank / not found.
        """
        if not prc_name:
            return None
        query = Queries.get_costing_code(prc_name, branch)
        with HANAConnection() as conn:
            result = conn.execute(query)
        if result:
            return result[0].get("PrcCode")
        return None

    def find_duplicate_num_at_card(self, num_at_card, card_code, branch, table):
        """Return documents of ``table`` already holding this customer reference.

        Empty list when the reference is free (or blank -- a blank NumAtCard is
        never a duplicate; SAP only enforces the rule on a supplied value).
        """
        if not str(num_at_card or "").strip() or not card_code:
            return []
        query = Queries.get_duplicate_num_at_card(num_at_card, card_code, branch, table)
        with HANAConnection() as conn:
            return conn.execute(query) or []

    def get_series(self , finYear , groupCode, branch):
        with HANAConnection() as conn:
            query = Queries.get_series(finYear , groupCode, branch)
            result = conn.execute(query)

        return result

    def get_draft_verfication(self , refId, branch):
        with HANAConnection() as conn:
            query = Queries.get_draft_verification(refId, branch)
            result = conn.execute(query)

        return result

    def get_invoice_status(self , statusCode, branch):
        with HANAConnection() as conn:
            query = Queries.get_invoice_status(statusCode, branch)
            result = conn.execute(query)

        return result
    
    def get_docEntry(self , docNum, branch='OIL'):
        with HANAConnection() as conn:
            query = Queries.get_docEntry(docNum, branch)
            result = conn.execute(query)
            
        return result

    def get_order_doc_entry(self , docNum, branch='OIL'):
        with HANAConnection() as conn:
            query = Queries.get_order_doc_entry(docNum, branch)
            result = conn.execute(query)

        return result
