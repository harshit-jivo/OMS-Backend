# views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .services.services import SalesOrderService
from .utils import group_sales_orders

class GetSalesOrderView(APIView):
    def get(self, request):
        party_code = request.query_params.get('card_code')

        if not party_code:
            return Response(
                {"error": "card_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        rows = SalesOrderService().syncSalesOrder(party_code)
        grouped = group_sales_orders(rows)
        return Response(grouped)
    
    
class GetOpenPartiesView(APIView):
    def get (self , request):
        openParties = SalesOrderService().syncOpenParties()
        return Response(openParties)
    
class GetCustomerDetailsView(APIView):
    def get (self , request):
        party_code = request.query_params.get('card_code')

        if not party_code:
            return Response(
                {"error": "card_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        details = SalesOrderService().getCustomerDetails(party_code)
        return Response(details)

class GetWarehouseDetailsView(APIView):
    def get (self , request):
        warehouse_code = request.query_params.get('whs_code')

        if not warehouse_code:
            return Response(
                {"error": "whs_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        details = SalesOrderService().getWarehouseDetails(warehouse_code)
        return Response(details)

class GetSalespersonDetailsView(APIView):
    def get (self , request):
        salesperson_code = request.query_params.get('slp_code')

        if not salesperson_code:
            return Response(
                {"error": "slp_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        details = SalesOrderService().getSalespersonDetails(salesperson_code)
        return Response(details)

class GetFreightMastersView(APIView):
    def get (self , request):
        freight_masters = SalesOrderService().FreightMasters()
        return Response(freight_masters)
    
class GetAddressView(APIView):
    def get (self , request):
        card_code = request.query_params.get('card_code')

        if not card_code:
            return Response(
                {"error": "card_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        address = SalesOrderService().getAddress(card_code)
        return Response(address)
    
    
class GetVendorStatesView(APIView):
    def get (self , request):
        states = SalesOrderService().getVendorStates()
        return Response(states)
    

class GetStateChainView(APIView):
    def get (self , request):
        state_code = request.query_params.get('state_code')


        chain = SalesOrderService().getStateChain(state_code)
        return Response(chain)
    
class GetAllCustomersView(APIView):
    def get (self , request):
        customers = SalesOrderService().getAllCustomers()
        return Response(customers)
    
class GetNextDocNumberView(APIView):
    def get (self , request):
        doc_type = request.query_params.get('doc_type')
    
        if not doc_type:
            return Response(
                {"error": "doc_type is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        next_doc_number = SalesOrderService().getNextDocNum(doc_type)
        return Response(next_doc_number)
    
class GetFGItemsView(APIView):
    def get (self , request):
        fg_items = SalesOrderService().getFGItems()
        return Response(fg_items)
    
    
class GetBatchDetailsView(APIView):
    def get (self , request):
        item_code = request.query_params.get('item_code')
        whs_code = request.query_params.get('whs_code')

        if not item_code or not whs_code:
            return Response(
                {"error": "item_code and whs_code are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        batch_details = SalesOrderService().get_batch_details(item_code , whs_code)
        return Response(batch_details)
    
class GetInventoryDetailsView(APIView):
    def get (self , request):
        item_code = request.query_params.get('item_code')

        if not item_code:
            return Response(
                {"error": "item_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        inventory_details = SalesOrderService().get_inventory_details(item_code)
        return Response(inventory_details)
    
class GetItemPriceView(APIView):
    def get (self , request):
        item_code = request.query_params.get('item_code')
        price_list = request.query_params.get('price_list')

        if not item_code or not price_list:
            return Response(
                {"error": "item_code and price_list are required"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        item_price = SalesOrderService().get_item_price(item_code , price_list)
        return Response(item_price)