from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from .models import InvoiceLog , InvocieHistory
from .serializers import InvoiceLogSerializer ,InvoiveHistorySerializer
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
    
class InvoicelogStatusUpdateView(APIView):
    def patch(self, request, pk):
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        if new_status not in dict(InvoiceLog.STATUS_CHOICES):
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)
        if new_status == 'REJECTED' and not request.data.get('rejection_reason'):
            return Response({'error': 'Rejection reason is required when status is REJECTED'}, status=status.HTTP_400_BAD_REQUEST)
        if new_status == 'REJECTED':
            invoice_log.rejection_reason = request.data.get('rejection_reason')
        
        invoice_log.status = new_status
        invoice_log.save()
        return Response({'message': 'Status updated successfully'}, status=status.HTTP_200_OK)
    
    
class InvoiceLogListView(APIView):
    
    def get(self , request):
        status = request.query_params.get('status')
        if status:
            invoice_logs = InvoiceLog.objects.filter(status=status)
        else:
            invoice_logs = InvoiceLog.objects.all()

        serializer = InvoiceLogSerializer(invoice_logs, many=True)
        return Response(serializer.data)

class InvoiceHistoryView(APIView):
    
    def get(self ,  request , pk):
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        history = invoice_log.history.all()
        serializer = InvoiveHistorySerializer(history, many=True)
        return Response(serializer.data)
