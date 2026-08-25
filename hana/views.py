# views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .services.services import SalesOrderService
from .utils import group_sales_orders

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

class GetWarehousesView(APIView):
    """Selectable warehouses, for the order-level warehouse picker."""

    def get(self, request):
        branch, error = get_branch_or_error(request)
        if error:
            return error

        return Response(SalesOrderService().getWarehouses(branch))


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
