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