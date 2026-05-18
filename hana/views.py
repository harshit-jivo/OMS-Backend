from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response

from .services.services import SalesOrderService

class getSalesOrderView(APIView):
    def get(self , request):
        result = SalesOrderService().syncSalesOrder()
        return Response({"so" : result})