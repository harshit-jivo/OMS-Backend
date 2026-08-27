"""AI order summary.

Split out of `orders/views.py` (plan item 3.1). One view over
`orders.ai_service`. It is separate because it is the only endpoint in the app
that calls an external LLM, so its failure modes and cost are nothing like the
rest of `orders`.
"""
from urllib import request
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from orders.ai_service import get_order_summary



class AiOrderSummaryView(APIView):
    """Order summary text for a supplied order payload.

    Was a plain Django view decorated `@csrf_exempt`, which made it the most
    exposed endpoint in the project: no authentication (a plain view never
    enters DRF's dispatch, so no permission class could apply), no CSRF, and an
    unguarded `json.loads(request.body)` that raised a 500 on any malformed
    body.

    `get_order_summary` is still a stub returning a formatted string, and no
    client calls this — so requiring a login costs nothing today and stops the
    endpoint from becoming an unauthenticated proxy to a paid AI service the
    moment someone wires the real one in behind it.
    """

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response(
                {'success': False, 'message': 'A JSON object body is required'},
                status=status.HTTP_400_BAD_REQUEST)
        return Response({'summary': get_order_summary(request.data)})
