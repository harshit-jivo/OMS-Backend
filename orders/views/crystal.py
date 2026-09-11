"""Sales order print, rendered by the Crystal Reports service.

The counterpart of `invoice.views.GetPrintReport`: same query parameters, same
PDF response, `ORDR` instead of `OINV`. Nothing in the web or mobile client
calls this yet — it exists so a sales-order print screen can be built against a
route that already works.

Which layout renders is decided entirely on the service side, by the company in
the path: `Company.{key}.Report.order` in its `Web.config` maps oil/bev/mart to
`SO_OIL.rpt` / `SO_BEVERAGE.rpt` / `SO_MART.rpt`.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import crystal
from hana.utils import normalize_branch, resolve_order_doc_entry


class SalesOrderPrintView(APIView):
    # DELIBERATELY NOT AllowAny, which is what the invoice bill print next door
    # uses. That route predates `core.tests.PublicEndpointAllowlistTests` and is
    # one of the offenders it still reports; this one has no client yet, so there
    # is nothing to keep working by repeating the mistake. A sales order PDF
    # carries the customer, their address and their prices, and DocEntry is a
    # sequential integer — anonymous access here is an enumerable leak.
    #
    # The cost is that `window.open` and `<iframe src>` cannot reach it: neither
    # attaches the Authorization header, and the API issues no session cookie.
    # A print screen must therefore fetch through axios and render an object URL,
    # the way `einvoice.views.irn_qr_png`'s allowlist entry describes. If that
    # turns out to be the wrong trade, the change is to add this path to
    # `PUBLIC_ROUTES` in core/tests.py WITH the reason — not to quietly relax it.
    permission_classes = [IsAuthenticated]

    # Each company renders through its own path on the Crystal service, which
    # maps it to that company's ODBC DSN and HANA schema. Unlike the invoice
    # routes there is no legacy company-less form to keep working, so OIL is
    # addressed explicitly here too.
    _CRYSTAL_PATHS = {
        'OIL': 'api/salesorder/oil',
        'BEVERAGE': 'api/salesorder/bev',
        'MART': 'api/salesorder/mart',
    }

    def get(self, request):
        doc_num = request.query_params.get('docNum')
        # Which company's order this is: it picks both the schema the DocNum is
        # resolved against and the Crystal path the PDF is rendered from.
        # Defaults to OIL, as the invoice route does.
        branch = normalize_branch(request.query_params.get('branch'))
        if branch is None or branch not in self._CRYSTAL_PATHS:
            return Response(
                {'error': 'branch must be one of: '
                          + ', '.join(sorted(self._CRYSTAL_PATHS))},
                status=status.HTTP_400_BAD_REQUEST)

        # A caller already holding ORDR."DocEntry" skips the lookup — that is
        # what the Crystal service wants anyway. Note this is the internal key,
        # not the order number a user reads off the document.
        doc_entry = (request.query_params.get('docEntry') or '').strip()

        if not doc_entry:
            if not doc_num:
                return Response({'error': 'docNum is required'},
                                status=status.HTTP_400_BAD_REQUEST)

            doc_entry = resolve_order_doc_entry(doc_num, branch)
            if not doc_entry:
                return Response(
                    {'error': f'No {branch} sales order found for docNum {doc_num}'},
                    status=status.HTTP_404_NOT_FOUND)

        return crystal.render_pdf(
            self._CRYSTAL_PATHS[branch],
            doc_entry,
            crystal.download_name(doc_num or doc_entry,
                                  request.query_params.get('party')),
            ascii_fallback='sales-order.pdf',
        )
