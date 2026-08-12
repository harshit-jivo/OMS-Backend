# views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from orders.models import Order
from sap_sync.models import SalesOrderLog

from .services.services import SalesOrderService
from .utils import build_inventory_report, build_pending_dispatch, group_sales_orders

VALID_BRANCHES = ('OIL', 'BEVERAGE')


def get_branch_or_error(request):
    branch = request.query_params.get('branch')
    if branch not in VALID_BRANCHES:
        return None, Response(
            {"error": "branch is required and must be one of: OIL, BEVERAGE"},
            status=status.HTTP_400_BAD_REQUEST
        )
    return branch, None


class GetProductStockView(APIView):
    def get(self, request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        try:
            products = SalesOrderService().getProductStock(branch)
            return Response(products)
        except Exception as error:
            return Response(
                {
                    "error": "Unable to fetch product stock from HANA.",
                    "detail": str(error),
                },
                status=status.HTTP_502_BAD_GATEWAY
            )

class GetInventoryReportView(APIView):
    """Warehouse-wise FG stock, pivoted and grouped by variety.

    Feeds the billing Inventory Report page (and its Excel download): one row
    per item, one column per warehouse, a subtotal per variety.
    """

    def get(self, request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        # ?warehouses=BH-BT,BH-PF narrows the report (and every total in it) to
        # the warehouses billing actually cares about.
        raw = request.query_params.get('warehouses') or ''
        whs_codes = [code.strip() for code in raw.split(',') if code.strip()]

        try:
            rows = SalesOrderService().getInventoryReport(branch, whs_codes or None)
        except Exception as error:
            return Response(
                {
                    "error": "Unable to fetch inventory from HANA.",
                    "detail": str(error),
                },
                status=status.HTTP_502_BAD_GATEWAY
            )

        report = build_inventory_report(rows)
        report['branch'] = branch
        return Response(report)


def _dispatch_from_map(rows):
    """SAP sales-order DocNum -> the OMS dispatch location it was raised from.

    OMS pushes its orders to SAP and records the resulting document in
    SalesOrderLog, which is the only link back: ORDR carries no OMS reference
    for sales orders. Orders keyed in SAP directly simply have no entry here,
    and the caller falls back to the line's warehouse.
    """
    doc_nums = {row['sales_order'] for row in rows if row.get('sales_order')}
    if not doc_nums:
        return {}

    # order_id is a CharField holding the OMS order's PK as text.
    log_pairs = (
        SalesOrderLog.objects
        .filter(sap_doc_num__in=doc_nums, status='SUCCESS')
        .exclude(order_id__isnull=True)
        .values_list('sap_doc_num', 'order_id')
    )
    order_by_doc = {}
    for doc_num, order_id in log_pairs:
        if str(order_id).isdigit():
            order_by_doc[str(doc_num)] = int(order_id)
    if not order_by_doc:
        return {}

    locations = dict(
        Order.objects
        .filter(id__in=set(order_by_doc.values()))
        .values_list('id', 'dispatch_from_name')
    )
    return {
        doc_num: locations.get(order_id) or ""
        for doc_num, order_id in order_by_doc.items()
    }


class GetPendingDispatchView(APIView):
    """Open sales orders against the AR invoices raised on them.

    The billing "Sales Order vs AR Invoice" report. Read wholly out of SAP --
    order lines, invoices and the links between them -- so it reflects the
    moment an invoice is punched or a line is added, with nothing to keep in
    step by hand.
    """

    def get(self, request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        from_date = request.query_params.get('from_date')
        to_date = request.query_params.get('to_date')

        try:
            lines, invoices = SalesOrderService().getPendingDispatch(
                branch, from_date, to_date
            )
        except Exception as error:
            return Response(
                {
                    "error": "Unable to fetch pending sales orders from HANA.",
                    "detail": str(error),
                },
                status=status.HTTP_502_BAD_GATEWAY
            )

        orders = build_pending_dispatch(lines, invoices, _dispatch_from_map(lines))
        return Response({
            "branch": branch,
            "order_count": len(orders),
            "line_count": sum(order['pending_line_count'] for order in orders),
            "invoice_count": sum(order['invoice_count'] for order in orders),
            "orders": orders,
        })


class GetSalesOrderView(APIView):
    def get(self, request):
        party_code = request.query_params.get('card_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not party_code:
            return Response(
                {"error": "card_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        rows = SalesOrderService().syncSalesOrder(party_code, branch)
        grouped = group_sales_orders(rows)
        return Response(grouped)

class GetProductSalesOrderView(APIView):
    def get(self, request):
        item_code = request.query_params.get('item_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not item_code:
            return Response(
                {"error": "item_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        rows = SalesOrderService().syncSalesOrderByProduct(item_code, branch)
        grouped = group_sales_orders(rows)
        return Response(grouped)


class GetOpenPartiesView(APIView):
    def get (self , request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        openParties = SalesOrderService().syncOpenParties(branch)
        return Response(openParties)

class GetCustomerDetailsView(APIView):
    def get (self , request):
        party_code = request.query_params.get('card_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not party_code:
            return Response(
                {"error": "card_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        details = SalesOrderService().getCustomerDetails(party_code, branch)
        return Response(details)

class GetWarehouseDetailsView(APIView):
    def get (self , request):
        warehouse_code = request.query_params.get('whs_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not warehouse_code:
            return Response(
                {"error": "whs_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        details = SalesOrderService().getWarehouseDetails(warehouse_code, branch)
        return Response(details)

class GetSalespersonDetailsView(APIView):
    def get (self , request):
        salesperson_code = request.query_params.get('slp_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not salesperson_code:
            return Response(
                {"error": "slp_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        details = SalesOrderService().getSalespersonDetails(salesperson_code, branch)
        return Response(details)

class GetFreightMastersView(APIView):
    def get (self , request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        freight_masters = SalesOrderService().FreightMasters(branch)
        return Response(freight_masters)

class GetAddressView(APIView):
    def get (self , request):
        card_code = request.query_params.get('card_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not card_code:
            return Response(
                {"error": "card_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        address = SalesOrderService().getAddress(card_code, branch)
        return Response(address)


class GetVendorStatesView(APIView):
    def get (self , request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        states = SalesOrderService().getVendorStates(branch)
        return Response(states)


class GetStateChainView(APIView):
    def get (self , request):
        state_code = request.query_params.get('state_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        chain = SalesOrderService().getStateChain(branch, state_code)
        return Response(chain)

class GetAllCustomersView(APIView):
    def get (self , request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        customers = SalesOrderService().getAllCustomers(branch)
        return Response(customers)

class GetNextDocNumberView(APIView):
    def get (self , request):
        doc_type = request.query_params.get('doc_type')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not doc_type:
            return Response(
                {"error": "doc_type is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        next_doc_number = SalesOrderService().getNextDocNum(doc_type, branch)
        return Response(next_doc_number)

class GetFGItemsView(APIView):
    def get (self , request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        fg_items = SalesOrderService().getFGItems(branch)
        return Response(fg_items)


class GetBatchDetailsView(APIView):
    def get (self , request):
        item_code = request.query_params.get('item_code')
        whs_code = request.query_params.get('whs_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not item_code or not whs_code:
            return Response(
                {"error": "item_code and whs_code are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        batch_details = SalesOrderService().get_batch_details(item_code , whs_code, branch)
        return Response(batch_details)

class GetInventoryDetailsView(APIView):
    def get (self , request):
        item_code = request.query_params.get('item_code')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not item_code:
            return Response(
                {"error": "item_code is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        inventory_details = SalesOrderService().get_inventory_details(item_code, branch)
        return Response(inventory_details)

class GetItemPriceView(APIView):
    def get (self , request):
        item_code = request.query_params.get('item_code')
        price_list = request.query_params.get('price_list')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not item_code or not price_list:
            return Response(
                {"error": "item_code and price_list are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        item_price = SalesOrderService().get_item_price(item_code , price_list, branch)
        return Response(item_price)

class GetSeries(APIView):
    def get(self , request):
        finYear = request.query_params.get('finYear')
        groupCode = request.query_params.get('groupCode')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not finYear or not groupCode:
            return Response({"error" : "Finanical month and year and GroupCode / BPLId is  require"} , status = status.HTTP_400_BAD_REQUEST)

        series_detail = SalesOrderService().get_series(finYear , groupCode, branch)
        return Response(series_detail)


class GetDraftVerification(APIView):

    def get(self , request):
        refId = request.query_params.get('refId')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not refId:
            return Response({"error" : "Ref ID is Mandatory"} , status = status.HTTP_400_BAD_REQUEST)

        result = SalesOrderService().get_draft_verfication(refId, branch)
        return Response({"data" : result})

class GetInvoiceDrafts(APIView):

    def get(self , request):
        statusCode = request.query_params.get('statusCode')
        branch, error = get_branch_or_error(request)
        if error:
            return error

        if not statusCode:
            return Response({"error" : "StatusCode is Mandatory"} , status = status.HTTP_400_BAD_REQUEST)

        result = SalesOrderService().get_invoice_status(statusCode, branch)
        return Response({"data" : result})
