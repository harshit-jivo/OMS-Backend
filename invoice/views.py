from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import InvoiceLog , InvoiceRefLogs
from .serializers import InvoiceLogSerializer , InvoiceRefLogsSerializer
from rest_framework.permissions import IsAuthenticated , AllowAny
from rest_framework import status
from rest_framework.generics import CreateAPIView, ListAPIView


class InvoiceLogCreateView(APIView):
    def post(self, request):
        serializer = InvoiceLogSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    
class InvoiceLogListView(APIView):
    
    def get(self , request):
        status = request.query_params.get('status')
        if status:
            invoice_logs = InvoiceLog.objects.filter(status=status)
        else:
            invoice_logs = InvoiceLog.objects.all()

        serializer = InvoiceLogSerializer(invoice_logs, many=True)
        return Response(serializer.data)



class InvoiceRefLogCreateView(CreateAPIView):
    
    serializer_class = InvoiceRefLogsSerializer
    queryset = InvoiceRefLogs.objects.all()