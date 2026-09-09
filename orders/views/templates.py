"""Saved order templates: a user's remembered orders, and re-placing them.

Split out of `orders/views.py` (plan item 3.1). The rule for whether a new
order duplicates a saved template lives in
`orders.services.order_templates`; these two views only list what is saved.
"""
from urllib import request
from orders.models import Template
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status




class TemplatePartyListView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
  
        templates = Template.objects.filter(user=request.user).select_related('order')
    
        parties_dict = {}
        for t in templates:
            card_code = t.order.card_code
            if card_code not in parties_dict:
                parties_dict[card_code] = {
                    "label": f"{t.order.card_name}",
                    # "label": f"{t.order.card_name} ({card_code})",
                    "value": card_code
                }
    
        return Response(list(parties_dict.values()), status=status.HTTP_200_OK)


class TemplateOrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        card_code = request.query_params.get('card_code')
        if not card_code:
            return Response({"error": "card_code is required"}, status=status.HTTP_400_BAD_REQUEST)
    
        templates = Template.objects.filter(
            user=request.user,
            order__card_code=card_code
        ).select_related('order').order_by('-created_at')
    
        orders_data = []
        for t in templates:
            date_str = t.order.created_at.strftime('%d-%b-%Y') if t.order.created_at else 'Unknown Date'
            orders_data.append({
                "label": f"Order #{t.order.order_number} ({date_str}) - ₹{t.order.total_amount}",
                "value": t.order.id
            })
        
        return Response(orders_data, status=status.HTTP_200_OK)
