"""Pre-order stock check.

Split out of `orders/views.py` (plan item 3.1). The view parses the request and
shapes the answer; `orders.services.stock_check` decides what a line needs and
reads live stock from HANA.
"""
from urllib import request
from orders.models import Order
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from collections import defaultdict
from ._shared import (
    _get_base_orders,
)
from orders.services.stock_check import (
    _get_live_stock_by_product,
    _stock_check_key,
    _stock_check_required_qty,
)




class OrderStockCheckView(APIView):

    def get(self, request):
        if request.user.is_authenticated:
            orders = _get_base_orders(request.user)
        else:
            orders = Order.objects.all()

        orders = (
            orders
            .filter(sap_created=True)
            .select_related('status')
            .prefetch_related('items')
            .order_by('-created_at')
            .distinct()
        )

        item_codes_by_category = defaultdict(set)
        for order in orders:
            for item in order.items.all():
                item_code = str(getattr(item, 'item_code', '') or '').strip()
                category = str(getattr(item, 'category', '') or '').strip().upper()
                if item_code and category:
                    item_codes_by_category[category].add(item_code)

        try:
            stock_by_product = _get_live_stock_by_product(item_codes_by_category)
        except Exception as error:
            return Response(
                {
                    'error': 'Unable to fetch live stock from SAP.',
                    'detail': str(error),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        data = []
        for order in orders:
            items = []
            for item in order.items.all():
                key = _stock_check_key(item.item_code, item.category)
                items.append({
                    'item_code': item.item_code or '-',
                    'item_name': item.item_name or item.item_code or '-',
                    'category': item.category or '-',
                    'required_qty': _stock_check_required_qty(item),
                    'available_stock': stock_by_product.get(key, 0),
                })

            data.append({
                'id': order.id,
                'order_number': order.order_number,
                'date': order.created_at,
                'customer': (
                    order.employee_id or order.card_name or 'Staff'
                    if order.order_type == 'STAFF'
                    else order.card_name or order.card_code or '-'
                ),
                'order_type': 'Staff' if order.order_type == 'STAFF' else 'Party',
                'dispatch_from': order.dispatch_from_name or str(order.dispatch_from_id or '-'),
                'status': getattr(order.status, 'name', '-') or '-',
                'items': items,
            })

        return Response(data)
