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