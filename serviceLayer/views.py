from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from .service import SAPServiceLayerManager
from rest_framework import status
from django.conf import settings

class SAPInvoiceCreateView(APIView):
    
    def post(self , request , *args, **kwargs):
        
        invoice_payload = request.data
        invoice_url = f"{settings.HANA_SERVICE_LAYER_URL}/Invoices"
        
        try:
            session = SAPServiceLayerManager.get_session()
            sap_response = session.post(invoice_url , json = invoice_payload , timeout = 20)
            
            if sap_response.status_code == 401:
                SAPServiceLayerManager.clear_session()
                session = SAPServiceLayerManager.get_session()
                sap_response = session.post(invoice_url , json = invoice_payload , timeout = 20)
                
            if sap_response.status_code in [200 , 201]:
                return Response(sap_response.json(), status=status.HTTP_201_CREATED)
            
            return Response({"error": "SAP Error", "details": sap_response.json()}, status=sap_response.status_code)
        
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
                
                
class DraftCreateView(APIView):
    
    def post(self , request , *args, **kwargs):
        
        draft_payload = request.data
        draft_url = f"{settings.HANA_SERVICE_LAYER_URL}/Drafts"
        
        try:
            session = SAPServiceLayerManager.get_session()
            sap_response = session.post(draft_url , json = draft_payload , timeout = 20)
            
            if sap_response.status_code == 401:
                SAPServiceLayerManager.clear_session()
                session = SAPServiceLayerManager.get_session()
                sap_response = session.post(draft_url , json = draft_payload , timeout = 20)
                
            if sap_response.status_code in [200 , 201]:
                return Response(sap_response.json(), status=status.HTTP_201_CREATED)
            
            return Response({"error": "SAP Error", "details": sap_response.json()}, status=sap_response.status_code)
        
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
class DraftApproveView(APIView):

    def post(self , request , *args , **kwargs):
        approve_payload =  request.data
        approval_id = request.query_params.get("approval_id")

        if not approval_id:
            return Response({"error": "Approval Id is Mandatory"}, status=status.HTTP_400_BAD_REQUEST)

        approve_url = f"{settings.HANA_SERVICE_LAYER_URL}/ApprovalRequests({approval_id})"

        try:
            session = SAPServiceLayerManager.get_session()
            sap_response = session.patch(approve_url , json=approve_payload , timeout=20)

            if sap_response.status_code == 401:
                SAPServiceLayerManager.clear_session()
                session = SAPServiceLayerManager.get_session()
                sap_response = session.patch(approve_url , json=approve_payload , timeout=20)

            if sap_response.status_code in [200 , 201, 204]:
                 return Response({"status": "approved", "approval_id": approval_id}, status=status.HTTP_200_OK)

            return Response({"error": "SAP Error", "details": sap_response.json()}, status=sap_response.status_code)
        
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        