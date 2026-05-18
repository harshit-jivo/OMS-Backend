from django.utils import timezone
from .connection import HANAConnection , Queries

class SalesOrderService():

    def syncSalesOrder(self):
        with HANAConnection() as conn:
            query = Queries.get_sales_orders()
            result = conn.execute(query)
            
        return result