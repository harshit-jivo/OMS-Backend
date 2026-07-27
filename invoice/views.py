import json
import requests

from django.conf import settings
from django.utils import timezone
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from .models import InvoiceLog , InvocieHistory , InvoiceRefLogs , CreditLimitLogs
from .serializers import InvoiceLogSerializer ,InvoiveHistorySerializer , InvoiceRefLogsSerializer , CreditLimitLogSerializer
from rest_framework.permissions import IsAuthenticated , AllowAny
from rest_framework import status
from rest_framework import generics
from rest_framework.generics import CreateAPIView, ListAPIView
from django.http import HttpResponse

from hana.services.services import SalesOrderService



class InvoiceLogCreateView(APIView):
    def post(self, request):
        serializer = InvoiceLogSerializer(data=request.data)
        if serializer.is_valid():
            invoice_log_instance = serializer.save(created_by=request.user)
            InvocieHistory.objects.create(
                invoice_log=invoice_log_instance,
                so_number=invoice_log_instance.so_number,
                party_name=invoice_log_instance.party_name,
                total_amount=invoice_log_instance.total_amount,
                status=invoice_log_instance.status,
                invoice_payload=invoice_log_instance.invoice_payload,
                created_by=request.user 
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
class InvoicelogStatusUpdateView(APIView):

    def patch(self, request, pk):
        try:
            invoice_log = InvoiceLog.objects.get(pk=pk)
        except InvoiceLog.DoesNotExist:
            return Response({'error': 'Invoice log not found'}, status=status.HTTP_404_NOT_FOUND)

        new_status = request.data.get('status')
        user = request.data.get('user')
        
        if new_status not in dict(InvoiceLog.STATUS_CHOICES):
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)
        if new_status == 'REJECTED' and not request.data.get('rejection_reason'):
            return Response({'error': 'Rejection reason is required when status is REJECTED'}, status=status.HTTP_400_BAD_REQUEST)
        if new_status == 'REJECTED':
            invoice_log.rejection_reason = request.data.get('rejection_reason')
        if new_status == 'ERROR' and request.data.get('error_message'):
            invoice_log.error_message = request.data.get('error_message')

        invoice_log.status = new_status
        InvocieHistory.objects.create(
                    invoice_log=invoice_log,
                    so_number=invoice_log.so_number,
                    party_name=invoice_log.party_name,
                    total_amount=invoice_log.total_amount,
                    status=invoice_log.status,
                    invoice_payload=invoice_log.invoice_payload,
                    created_by=user
        )
        print(f"Creator{invoice_log.created_by}")
        print(f"Approver{request.user}")
        invoice_log.save()
        return Response({'message': 'Status updated successfully'}, status=status.HTTP_200_OK)
    
    
class InvoiceLogListView(APIView):
    
  def get(self, request):
    inv_status = request.query_params.get('status')
    warehouse = request.query_params.get('whs')
    if not warehouse:
        return Response(
            {'error': 'Warehouse Code is a required parameter.'}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    invoice_logs = InvoiceLog.objects.filter(warehouse=warehouse)
    
    if inv_status:
        invoice_logs = invoice_logs.filter(status=inv_status) 

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


class InvoiceRefLogCreateView(CreateAPIView):

    serializer_class = InvoiceRefLogsSerializer
    queryset = InvoiceRefLogs.objects.all()

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        fields = dict(serializer.validated_data)
        ref_id = fields.pop('ref_id')

        # SAP's Service Layer sometimes returns "matching record not found" even
        # though the invoice draft was actually created. Each draft is stamped
        # with our ref number in ODRF."U_OMS_REF", so verify against HANA before
        # trusting that error: if a draft with this ref exists, it was created.
        verification_error = None
        draft_exists = False
        try:
            draft_exists = bool(SalesOrderService().get_draft_verfication(ref_id))
        except Exception as exc:
            # Verification unavailable (e.g. HANA unreachable) - log the attempt
            # as submitted and let the caller know it could not be confirmed.
            verification_error = str(exc)

        if verification_error is not None:
            message = "Invoice ref logged, but HANA verification is currently unavailable."
        elif draft_exists:
            # Draft really is in SAP - record success regardless of any
            # "matching record not found" error the client may have seen.
            fields['status'] = 'SUCCESS'
            fields['error_message'] = None
            message = "Invoice created"
        else:
            message = "No matching draft found in HANA for this ref number."

        # Each ref number maps to exactly one invoice draft, so keep the log
        # idempotent: a repeat submit (double-click / retry / StrictMode) updates
        # the existing row instead of inserting a duplicate.
        instance, created = InvoiceRefLogs.objects.update_or_create(
            ref_id=ref_id,
            defaults=fields,
        )

        return Response(
            {
                "message": message,
                "verified": draft_exists,
                "duplicate": not created,
                "verification_error": verification_error,
                "data": self.get_serializer(instance).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class UpdateInvoiceLogView(generics.RetrieveUpdateAPIView):
    queryset = InvoiceLog.objects.all()
    serializer_class = InvoiceLogSerializer
    lookup_field = 'id'

    def perform_update(self, serializer):
        invoice_log_instance = serializer.save()

        InvocieHistory.objects.create(
            invoice_log=invoice_log_instance,
            so_number=invoice_log_instance.so_number,
            party_name=invoice_log_instance.party_name,
            total_amount=invoice_log_instance.total_amount,
            status=invoice_log_instance.status,
            invoice_payload=invoice_log_instance.invoice_payload,
            created_by=self.request.user
        )


class CreditLimitCardsView(APIView):

    def get(self, request):
        company = request.query_params.get('company', '1')
        url = f"{settings.DSR_API_BASE}/api/CreditLimit/GetCustomerCards"
        try:
            dsr_response = requests.get(url, params={'company': company}, timeout=20, verify=False)
            try:
                body = dsr_response.json()
            except ValueError:
                body = {'error': dsr_response.text}
            return Response(body, status=dsr_response.status_code)
        except requests.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class CreditLimitRequestView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        document_data = request.data.get('documentData')
        attachment = request.FILES.get('attachment')

        invoice_log_id = request.data.get('invoice_log_id')
        if not document_data or not attachment:
            return Response(
                {'error': 'documentData and attachment are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            parsed = json.loads(document_data)
            parsed['createdBy'] = settings.OMS_JSAP_USER_ID
            document_data = json.dumps(parsed)
        except (ValueError, TypeError):
            return Response(
                {'error': 'documentData must be a valid JSON object'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        url = f"{settings.DSR_API_BASE}/api/CreditLimit/CreateCLDocumentV2"
        try:
            dsr_response = requests.post(
                url,
                data={'documentData': document_data},
                files={'attachment': (attachment.name, attachment, attachment.content_type)},
                timeout=30,
                verify=False,
            )
            try:
                body = dsr_response.json()
            except ValueError:
                body = {'message': dsr_response.text}

            invoice_log = InvoiceLog.objects.get(id=invoice_log_id)
            CreditLimitLogs.objects.create(
                invoice_log = invoice_log,
                jsap_doc_id = body["creditDocumentId"],
                party_name=invoice_log.party_name,
                created_by = request.user
            )


            return Response(body, status=dsr_response.status_code)
        except requests.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)



class GetCreditLimitJSAPFlow(APIView):
    def get(self, request):
        invoice_id = request.query_params.get('invoice_id')
        company_id = request.query_params.get('company')

        if not invoice_id or not company_id:
            return Response(
                {'error': 'Both invoice_id and company parameters are required.'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        jsap_doc = CreditLimitLogs.objects.filter(invoice_log_id=invoice_id).first()

        if not jsap_doc:
            return Response(
                {'error': 'No CreditLimitLogs entry found for the given invoice_id.'}, 
                status=status.HTTP_404_NOT_FOUND
            )

        doc_id = jsap_doc.jsap_doc_id
        formatted_month = timezone.now().strftime('%m-%Y') 

        request_payload = {
            "userId": settings.OMS_JSAP_USER_ID,
            "companyId": company_id,
            "month": formatted_month
        }

        url = f"{settings.DSR_API_BASE}/api/CreditLimit/GetAllDocuments"
        
        # TODO: Verify if this should be a different endpoint! 
        # In your original code, it was identical to the GetAllDocuments url.
        flow_url = f"{settings.DSR_API_BASE}/api/CreditLimit/GetApprovalFlow/" 
        
        try:
            # 1. Fetch All Documents
            dsr_response = requests.post(
                url,
                json=request_payload,
                timeout=30,
                verify=False,
            )
            
            if not dsr_response.ok:
                return Response(
                    {'error': 'Upstream DSR API error', 'details': dsr_response.text}, 
                    status=dsr_response.status_code
                )

            body = dsr_response.json()
            
            # Safely fetch data using .get() to avoid KeyError
            data = body.get("data", [])
            
            # 2. Extract flow_id safely
            flow_id = None  
            for rec in data:
                if rec.get("id") == doc_id:
                    flow_id = rec.get("flowId")
                    break

            if flow_id is None:
                return Response(
                    {'error': 'No flowId Associated found'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            flow_response = requests.get(
                flow_url, # Using flow_url here instead of url
                params={'flowId': flow_id}, 
                timeout=20, 
                verify=False
            )
            

            if not flow_response.ok:
                return Response(
                    {'error': 'Failed to fetch flow details', 'details': flow_response.text}, 
                    status=flow_response.status_code
                )

            flow_body = flow_response.json()
            
            return Response(flow_body, status=status.HTTP_200_OK)

        except requests.exceptions.Timeout:
            return Response({'error': 'DSR API timeout'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except ValueError:
            return Response({'error':'Invalid JSON received from DSR API'}, status=status.HTTP_502_BAD_GATEWAY)
        
class GetPrintReport(APIView):
    def get(self, request):
        docNum = request.query_params.get('docNum')
        if not docNum:
            return Response({"error": "docNum is required"}, status=status.HTTP_400_BAD_REQUEST)

        docEntry = SalesOrderService().get_docEntry(docNum)
        if not docEntry:
            return Response({"error": f"No invoice found for docNum {docNum}"},
                            status=status.HTTP_404_NOT_FOUND)

        url = f"{settings.CRYSTAL_URL}/api/billprint/{docEntry[0]['DocEntry']}"
        try:
            crystal_response = requests.get(url, timeout=60, verify=False)
        except requests.RequestException as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        if not crystal_response.ok:
            return Response({'error': 'Failed to generate print report',
                             'details': crystal_response.text},
                            status=crystal_response.status_code)

        resp = HttpResponse(
            crystal_response.content,
            status=crystal_response.status_code,
            content_type=crystal_response.headers.get('Content-Type', 'application/pdf'),
        )
        resp['Content-Disposition'] = f'inline; filename="invoice_{docNum}.pdf"'
       
        resp.xframe_options_exempt = True
        return resp

  
class InvoiceLogListwoWhsView(APIView):
    
  def get(self, request):
    inv_status = request.query_params.get('status')
    if inv_status:
        invoice_logs = InvoiceLog.objects.filter(status=inv_status) 
    else:
        invoice_logs = InvoiceLog.objects.all()
        
    serializer = InvoiceLogSerializer(invoice_logs, many=True)
    return Response(serializer.data)