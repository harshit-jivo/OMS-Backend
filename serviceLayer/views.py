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
        type = request.query_params.get('type')
        
        if not type:
            return Response({"error" : "Type(DRAFT / INVOICE) is Required"}  , status = status.HTTP_400_BAD_REQUEST)
        
        if type == 'DRAFT':
            user = settings.HANA_USERNAME
            password = settings.HANA_PASSWORD
            
        else:
            user = settings.SAP_APPROVER_USER
            password = settings.SAP_APPROVER_PASSWORD

        
        try:
            print(user)
            print(password)
            session = SAPServiceLayerManager.get_session_for(user , password )
            sap_response = session.post(invoice_url , json = invoice_payload , timeout = 20)
            
            if sap_response.status_code == 401:
                SAPServiceLayerManager.clear_session()
                session = SAPServiceLayerManager.get_session_for(user , password)
                sap_response = session.post(invoice_url , json = invoice_payload , timeout = 20)
                
            if sap_response.status_code in [200 , 201]:
                return Response(sap_response.json(), status=status.HTTP_201_CREATED)
            
            return Response({"error": "SAP Error", "details": sap_response.json()}, status=sap_response.status_code)
        
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
                
class DraftView(APIView):
    
    def post(self , request , *args, **kwargs):
        
        draft_payload = request.data
        draft_url = f"{settings.HANA_SERVICE_LAYER_URL}/Drafts"
        
        drafter_username = settings.HANA_USERNAME
        drafter_password = settings.HANA_PASSWORD
        
        print(drafter_username)
        print(drafter_password)
        try:
            session = SAPServiceLayerManager.get_session_for(drafter_username , drafter_password)
            sap_response = session.post(draft_url , json = draft_payload , timeout = 20)
            
            if sap_response.status_code == 401:
                SAPServiceLayerManager.clear_session()
                session = SAPServiceLayerManager.get_session(drafter_username , drafter_password)
                sap_response = session.post(draft_url , json = draft_payload , timeout = 20)
                
            if sap_response.status_code in [200 , 201]:
                return Response(sap_response.json(), status=status.HTTP_201_CREATED)
            
            return Response({"error": "SAP Error", "details": sap_response.json()}, status=sap_response.status_code)
        
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        
    def get(self , request , *args , **kwargs):
        
        draft_id = request.query_params.get('draft_id')
        draft_url = f"{settings.HANA_SERVICE_LAYER_URL}/Drafts({draft_id})"
        
        try:
            session = SAPServiceLayerManager.get_session()
            sap_response = session.get(draft_url, timeout = 20)
            
            if sap_response.status_code == 401:
                SAPServiceLayerManager.clear_session()
                session = SAPServiceLayerManager.get_session()
                sap_response = session.get(draft_url , timeout = 20)
                
            if sap_response.status_code in [200 , 201]:
                return Response(sap_response.json(), status=status.HTTP_201_CREATED)
            
            return Response({"error": "SAP Error", "details": sap_response.json()}, status=sap_response.status_code)
        
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
class DraftActionView(APIView):

    def post(self , request , *args , **kwargs):
        
        
        action_status =  request.query_params.get("status")
        draft_id = request.query_params.get("draft_id")
        approver_user = settings.SAP_APPROVER_USER
        approver_pass = settings.SAP_APPROVER_PASSWORD

        payload = {
          "ApprovalRequestDecisions": [
            {
              "Status": f"ard{action_status}",
              "Remarks": "Approved via OMS Portal"
            }
          ]
        }


        if not draft_id:
            return Response({"error": "Draft Id is Mandatory"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            session = SAPServiceLayerManager.get_session_for(approver_user, approver_pass)
            lookup_url = (
                f"{settings.HANA_SERVICE_LAYER_URL}/ApprovalRequests"
                f"?$filter=DraftEntry eq {draft_id}&$select=Code"
            )
            lookup_response = session.get(lookup_url, timeout=20)
            if lookup_response.status_code != 200:
                return Response({"error": "SAP Error", "details": lookup_response.json()}, status=lookup_response.status_code)

            approval_requests = lookup_response.json().get("value", [])
            if not approval_requests:
                return Response(
                    {"error": f"No approval request found for draft {draft_id}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            approval_code = approval_requests[0]["Code"]
            approve_url = f"{settings.HANA_SERVICE_LAYER_URL}/ApprovalRequests({approval_code})"

            sap_response = session.patch(approve_url , json=payload , timeout=20)

            if sap_response.status_code in [200 , 201, 204]:
                 return Response({"status": action_status.lower(), "draft_id": draft_id}, status=status.HTTP_200_OK)

            return Response({"error": "SAP Error", "details": sap_response.json()}, status=sap_response.status_code)
        
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        