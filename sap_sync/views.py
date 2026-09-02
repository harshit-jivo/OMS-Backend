"""SAP synchronisation API.

Every view in this module declared `permission_classes = [AllowAny]` — all 26
routes, including the ones that trigger a full master-data pull from SAP and
the two that approve sales orders. Those declarations are gone; the project
default (`IsAuthenticated`, set in OMS/settings.py) now applies.

The eight sync/schedule views are further restricted to administrators. They
are infrastructure controls, not business screens: `SyncAllView` rewrites the
product and party masters that every order is priced from, and the schedule
views decide when that happens unattended. An ordinary authenticated user has
no reason to reach them, and a mistaken trigger is expensive.

Deliberately NOT admin-gated: the approval views and every read endpoint.
Approval is a business decision made by non-admin approvers, and restricting it
here would break that flow — its own authorisation belongs with the order
workflow (plan Phase 2.6), not with a blanket role check.
"""
import logging
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.generics import ListAPIView, RetrieveAPIView
from django_filters.rest_framework import DjangoFilterBackend
from core.permissions import IsAdminRole
from django.db.models import Q
from core.pagination import OptInPagination, ordering_from
from .models import Product, Party, PartyAddress, SyncLog, SyncSchedule, Branch, SalesQuotationLog, active_product_q  , SalesOrderLog
from .serializers import (ProductSerializer, PartySerializer, PartyListSerializer,
    PartyAddressSerializer, SyncLogSerializer, SyncScheduleSerializer,BranchSerializer)
from .services import SyncService
from types import SimpleNamespace
from orders.models import Order

logger = logging.getLogger(__name__)

# ============ Sync Operations ============

class SyncAllView(APIView):
    """Trigger manual sync of all data (Products, Parties, Addresses)"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def post(self, request):
        try:
            sync_service = SyncService(triggered_by='manual')
            result = sync_service.sync_all()
            success = bool(result.get('success'))
            error_list = result.get('errors') or []
            message = (
                'Sync completed successfully'
                if success
                else f"Sync failed: {'; '.join(error_list)}" if error_list else 'Sync failed'
            )
            
            return Response({
                'success': success,
                'message': message,
                'data': result
            })
        except Exception as e:
            return Response({
                'success': False,
                'message': f'Sync failed: {str(e)}',
                'data': None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SyncProductsView(APIView):
    """Trigger manual sync of products only"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def post(self, request):
        try:
            sync_service = SyncService(triggered_by='manual')
            result = sync_service.sync_products()
            error_detail = result.get('error')
            message = (
                'Products sync completed'
                if result['success']
                else f'Products sync failed: {error_detail}' if error_detail else 'Products sync failed'
            )
            
            return Response({
                'success': result['success'],
                'message': message,
                'data': result
            })
        except Exception as e:
            return Response({
                'success': False,
                'message': f'Products sync failed: {str(e)}',
                'data': None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SyncPartiesView(APIView):
    """Trigger manual sync of parties only"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def post(self, request):
        try:
            sync_service = SyncService(triggered_by='manual')
            result = sync_service.sync_parties()
            error_detail = result.get('error')
            message = (
                'Parties sync completed'
                if result['success']
                else f'Parties sync failed: {error_detail}' if error_detail else 'Parties sync failed'
            )
            
            return Response({
                'success': result['success'],
                'message': message,
                'data': result
            })
        except Exception as e:
            return Response({
                'success': False,
                'message': f'Parties sync failed: {str(e)}',
                'data': None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SyncPartyAddressesView(APIView):
    """Trigger manual sync of party addresses only"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def post(self, request):
        try:
            sync_service = SyncService(triggered_by='manual')
            result = sync_service.sync_party_addresses()
            error_detail = result.get('error')
            message = (
                'Party addresses sync completed'
                if result['success']
                else f'Party addresses sync failed: {error_detail}' if error_detail else 'Party addresses sync failed'
            )
            
            return Response({
                'success': result['success'],
                'message': message,
                'data': result
            })
        except Exception as e:
            return Response({
                'success': False,
                'message': f'Party addresses sync failed: {str(e)}',
                'data': None
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============ Products ============

# Phase 6.3: the fields already handled above by hand (category, search,
# brand, exclude_deleted) keep their exact existing behaviour untouched.
# DjangoFilterBackend is wired in additively, for fields nothing filtered on
# before — it only ever acts on a query param a caller actually sends, so a
# request with none of these params is unaffected.
PRODUCT_FILTER_FIELDS = ['sub_group', 'type', 'variety']

# Allow-listed ?ordering= fields — see core.pagination.ordering_from. Applied
# only when the caller sends `ordering`; the model's own Meta.ordering
# (`item_code`) is otherwise left alone so an unordered request's response is
# unchanged.
PRODUCT_ORDER_FIELDS = {'item_code', 'item_name', 'category', 'brand', 'on_hand', 'created_at'}


class ProductListView(ListAPIView):
    serializer_class = ProductSerializer
    pagination_class = OptInPagination   # 4,162 rows
    filter_backends = [DjangoFilterBackend]
    filterset_fields = PRODUCT_FILTER_FIELDS

    def get_queryset(self):
        queryset = Product.objects.filter(active_product_q())

        category = self.request.query_params.get('category', None)
        if category:
            queryset = queryset.filter(category__iexact=category)

        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(
                Q(item_code__icontains=search) |
                Q(item_name__icontains=search)
            )

        brand = self.request.query_params.get('brand', None)
        if brand:
            queryset = queryset.filter(brand__icontains=brand)

        exclude_deleted = self.request.query_params.get('exclude_deleted', 'true')
        if exclude_deleted.lower() == 'true':
            queryset = queryset.exclude(is_deleted='Y')

        if self.request.query_params.get('ordering'):
            queryset = queryset.order_by(
                ordering_from(self.request, PRODUCT_ORDER_FIELDS, 'item_code'))

        return queryset


class ProductVarietyListView(APIView):

    def get(self, request):
        category = str(request.query_params.get('category') or '').strip()
        queryset = Product.objects.filter(active_product_q()).exclude(is_deleted='Y')
        if category:
            queryset = queryset.filter(category__iexact=category)

        sub_groups = sorted(
            {
                str(sub_group or '').strip()
                for sub_group in queryset.values_list('sub_group', flat=True)
                if str(sub_group or '').strip()
            },
            key=str.lower,
        )

        return Response({
            'category': category,
            'count': len(sub_groups),
            # `varieties` kept for backward compatibility; both now carry sub groups.
            'varieties': sub_groups,
            'sub_groups': sub_groups,
        })


class ProductDetailView(RetrieveAPIView):
    """Get single product by ID or item_code"""
    serializer_class = ProductSerializer
    queryset = Product.objects.filter(active_product_q())
    lookup_field = 'pk'


class ProductByCodeView(RetrieveAPIView):
    """Get product by item_code"""
    serializer_class = ProductSerializer
    queryset = Product.objects.filter(active_product_q())
    lookup_field = 'item_code'


# ============ Parties ============

# Additive, same reasoning as PRODUCT_FILTER_FIELDS above: fields the hand
# written filters below never touched, so no query param collides.
PARTY_FILTER_FIELDS = ['chain', 'country', 'category']

PARTY_ORDER_FIELDS = {'card_code', 'card_name', 'state', 'main_group', 'card_type'}


class PartyListView(ListAPIView):
    serializer_class = PartyListSerializer
    pagination_class = OptInPagination   # 3,357 rows
    filter_backends = [DjangoFilterBackend]
    filterset_fields = PARTY_FILTER_FIELDS

    def get_queryset(self):
        # Return all parties; filtering is handled on the client or via query params
        queryset = Party.objects.all()

        search = self.request.query_params.get('search', None)
        if search:
            queryset = queryset.filter(
                Q(card_code__icontains=search) |
                Q(card_name__icontains=search)
            )

        state = self.request.query_params.get('state', None)
        if state:
            queryset = queryset.filter(state__icontains=state)

        main_group = self.request.query_params.get('main_group', None)
        if main_group:
            queryset = queryset.filter(main_group__icontains=main_group)

        card_type = self.request.query_params.get('card_type', None)
        if card_type:
            queryset = queryset.filter(card_type=card_type)

        if self.request.query_params.get('ordering'):
            queryset = queryset.order_by(
                ordering_from(self.request, PARTY_ORDER_FIELDS, 'card_code'))

        return queryset

class PartyDetailView(RetrieveAPIView):
    """Get single party with addresses"""
    serializer_class = PartySerializer
    queryset = Party.objects.prefetch_related('addresses')
    lookup_field = 'pk'


class PartyByCodeView(RetrieveAPIView):
    """Get party by card_code with addresses"""
    serializer_class = PartySerializer
    queryset = Party.objects.prefetch_related('addresses')
    lookup_field = 'card_code'


# ============ Party Addresses ============

# Additive, same reasoning as PRODUCT_FILTER_FIELDS above: fields the hand
# written filters below never touched, so no query param collides.
PARTY_ADDRESS_FILTER_FIELDS = ['state', 'city', 'country', 'category']

PARTY_ADDRESS_ORDER_FIELDS = {'card_code', 'address_name', 'address_type', 'state', 'city'}


class PartyAddressListView(ListAPIView):
    """List all party addresses with optional filter"""
    serializer_class = PartyAddressSerializer
    # 35,719 rows unfiltered. Opt-in, so today's callers are unaffected;
    # see core/pagination.OptInPagination.
    pagination_class = OptInPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = PARTY_ADDRESS_FILTER_FIELDS

    def get_queryset(self):
        queryset = PartyAddress.objects.all()

        # Filter by card_code
        card_code = self.request.query_params.get('card_code', None)
        if card_code:
            queryset = queryset.filter(card_code=card_code)

        # Filter by address_type
        address_type = self.request.query_params.get('address_type', None)
        if address_type:
            queryset = queryset.filter(address_type=address_type)

        # Search by GST number
        gst = self.request.query_params.get('gst', None)
        if gst:
            queryset = queryset.filter(gst_number__icontains=gst)

        if self.request.query_params.get('ordering'):
            queryset = queryset.order_by(
                ordering_from(self.request, PARTY_ADDRESS_ORDER_FIELDS, 'card_code'))

        return queryset


# ============ Sync Logs ============

class SyncLogListView(ListAPIView):
    """List all sync logs"""
    serializer_class = SyncLogSerializer
    
    def get_queryset(self):
        queryset = SyncLog.objects.all()
        
        # Filter by sync_type
        sync_type = self.request.query_params.get('sync_type', None)
        if sync_type:
            queryset = queryset.filter(sync_type=sync_type)
        
        # Filter by status
        sync_status = self.request.query_params.get('status', None)
        if sync_status:
            queryset = queryset.filter(status=sync_status)
        
        # Limit results
        limit = self.request.query_params.get('limit', 50)
        try:
            limit = int(limit)
        except ValueError:
            limit = 50
        
        return queryset[:limit]


# ============ Schedule Management ============

class SyncScheduleListView(APIView):
    """List and create sync schedules"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def get(self, request):
        schedules = SyncSchedule.objects.all()
        serializer = SyncScheduleSerializer(schedules, many=True)
        return Response({
            'success': True,
            'data': serializer.data
        })
    
    def post(self, request):
        serializer = SyncScheduleSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Schedule created successfully',
                'data': serializer.data
            }, status=status.HTTP_201_CREATED)
        
        return Response({
            'success': False,
            'message': 'Failed to create schedule',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

class SyncScheduleDetailView(APIView):
    """Get, update, or delete a sync schedule"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def get_object(self, pk):
        try:
            return SyncSchedule.objects.get(pk=pk)
        except SyncSchedule.DoesNotExist:
            return None
    
    def get(self, request, pk):
        schedule = self.get_object(pk)
        if not schedule:
            return Response({
                'success': False,
                'message': 'Schedule not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        serializer = SyncScheduleSerializer(schedule)
        return Response({
            'success': True,
            'data': serializer.data
        })
    
    def put(self, request, pk):
        schedule = self.get_object(pk)
        if not schedule:
            return Response({
                'success': False,
                'message': 'Schedule not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        serializer = SyncScheduleSerializer(schedule, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Schedule updated successfully',
                'data': serializer.data
            })
        
        return Response({
            'success': False,
            'message': 'Failed to update schedule',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
    
    def delete(self, request, pk):
        schedule = self.get_object(pk)
        if not schedule:
            return Response({
                'success': False,
                'message': 'Schedule not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        schedule.delete()
        return Response({
            'success': True,
            'message': 'Schedule deleted successfully'
        })

class ToggleScheduleView(APIView):
    """Activate or deactivate a schedule"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def post(self, request, pk):
        try:
            schedule = SyncSchedule.objects.get(pk=pk)
        except SyncSchedule.DoesNotExist:
            return Response({
                'success': False,
                'message': 'Schedule not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        schedule.is_active = not schedule.is_active
        schedule.save()
        
        status_text = 'activated' if schedule.is_active else 'deactivated'
        
        return Response({
            'success': True,
            'message': f'Schedule {status_text} successfully',
            'data': SyncScheduleSerializer(schedule).data
        })

# ============ Sync Status ============

# Add this view
class BranchListView(ListAPIView):
    """Get all branches"""
    serializer_class = BranchSerializer
    queryset = Branch.objects.all().order_by('category', 'bpl_id')


# ---------------------------------------------------------------------------
# DISABLED 2026-08-27 — the Sales Quotation flow is closed and no longer used.
#
# Commented out rather than deleted, at the maintainers' request, so the
# implementation stays visible in place. `sap_sync.SalesQuotationLog` and its
# table are KEPT: the history stays queryable and nothing is dropped.
#
# Its routes are commented out in urls.py alongside this. Note that
# QuotationStatusView had already stopped working: it called
# `SalesOrderService().get_quotation_status(doc_entries)` without the required
# `branch` argument, so every call raised TypeError — caught by the `except
# Exception` below, which returns the same empty map it returns when SAP is
# unreachable. The breakage was indistinguishable from SAP being down, which is
# why nothing surfaced it.
#
# To restore: uncomment here and in urls.py, and fix that call by deciding
# which company DB to query.
# ---------------------------------------------------------------------------
# class SalesQuotationLogByOrderView(APIView):
#     """Get the latest successful SAP quotation log for an order."""
#     permission_classes = [IsAuthenticated]
#
#     def get(self, request, order_id):
#         quotation_log = (
#             SalesOrderLog.objects
#             .filter(order_id=str(order_id), status='SUCCESS', sap_doc_num__isnull=False)
#             .order_by('-created_at')
#             .first()
#         )
#
#         if not quotation_log:
#             return Response({
#                 'success': False,
#                 'message': 'Quotation log not found'
#             }, status=status.HTTP_404_NOT_FOUND)
#
#         return Response({
#             'success': True,
#             'data': {
#                 'order_id': quotation_log.order_id,
#                 'sap_doc_num': str(quotation_log.sap_doc_num),
#                 'sap_doc_entry': quotation_log.sap_doc_entry,
#                 'created_at': quotation_log.created_at.isoformat() if quotation_log.created_at else None,
#             }
#         })

class SyncBranchesView(APIView):
    """Sync branches from SAP"""
    permission_classes = [IsAuthenticated, IsAdminRole]
    
    def post(self, request):
        try:
            sync_service = SyncService(triggered_by=request.user.username)
            result = sync_service.sync_branches()
            
            return Response({
                'success': result['success'],
                'message': f"Synced {result['created'] + result['updated']} branches",
                'data': {
                    'processed': result['processed'],
                    'created': result['created'],
                    'updated': result['updated'],
                },
                'errors': [result['error']] if result.get('error') else None
            })
        except Exception as e:
            return Response({
                'success': False,
                'message': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class   ApproveOrderAPIView(APIView):
    """Approve an order and push to SAP"""
    
    def post(self, request):
        order_id = request.data.get('order_id')
        
        if not order_id:
            return Response({
                'success': False,
                'message': 'order_id is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            service = SyncService(triggered_by=request.user.username)
            order = Order.objects.get(id=order_id)
            result = service.create_sales_quotation(order)
            logger.info("Order %s approval result: %s", order_id, result)

            return Response({
                'success': True,
                'message': 'Order approved and pushed to SAP successfully',
                'data': result,
                'errors': None
            })
        except Order.DoesNotExist:
            return Response({
                'success': False,
                'message': 'Order not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                'success': False,
                'message': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
class SyncStatusView(APIView):
    """Get sync status with counts"""
    
    def get(self, request):
        try:
            # Get last sync and serialize manually
            last_sync = SyncLog.objects.order_by('-started_at').first()
            last_sync_data = None
            
            if last_sync:
                last_sync_data = {
                    'id': last_sync.id,
                    'sync_type': last_sync.sync_type,
                    'status': last_sync.status,
                    'records_processed': last_sync.records_processed,
                    'records_created': last_sync.records_created,
                    'records_updated': last_sync.records_updated,
                    'started_at': last_sync.started_at.isoformat() if last_sync.started_at else None,
                    'completed_at': last_sync.completed_at.isoformat() if last_sync.completed_at else None,
                    'triggered_by': last_sync.triggered_by,
                }
            
            return Response({
                'success': True,
                'data': {
                    'counts': {
                        'products': Product.objects.count(),
                        'parties': Party.objects.count(),
                        'addresses': PartyAddress.objects.count(),
                        'branches': Branch.objects.count(),
                    },
                    'last_sync': last_sync_data,  # ✅ Now serializable
                    'active_schedules': SyncSchedule.objects.filter(is_active=True).count(),
                }
            })
        except Exception as e:
            return Response({
                'success': False,
                'message': str(e),
            }, status=500)

# class PushSalesQuotationView(APIView):
#
#     def post(self, request):
#         order_id = request.data.get("order_id")
#
#         if not order_id:
#             return Response(
#                 {"error": "order_id is required"},
#                 status=status.HTTP_400_BAD_REQUEST
#             )
#
#         try:
#             order = Order.objects.get(id=order_id)
#
#             service = SyncService(triggered_by='manual')
#             sap_response = service.create_sales_quotation(order)
#            
#             return Response(
#                 {
#                     "message": "Quotation created successfully",
#                     "sap_response": sap_response
#                 },
#                 status=status.HTTP_200_OK
#             )
#
#         except Order.DoesNotExist:
#             return Response(
#                 {"error": "Order not found"},
#                 status=status.HTTP_404_NOT_FOUND
#             )
#
#         except Exception as e:
#             return Response(
#                 {"error": str(e)},
#                 status=status.HTTP_500_INTERNAL_SERVER_ERROR
#             )


class PushSalesOrderView(APIView):

    def post(self, request):
        order_id = request.data.get("order_id")

        if not order_id:
            return Response(
                {"error": "order_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            order = Order.objects.get(id=order_id)

            service = SyncService(triggered_by='manual')
            sap_response = service.create_sales_order(order)
            
            return Response(
                {
                    "message": "Quotation created successfully",
                    "sap_response": sap_response
                },
                status=status.HTTP_200_OK
            )

        except Order.DoesNotExist:
            return Response(
                {"error": "Order not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR

            )

# class TestSalesQuotation(APIView):
#    
#     def post(self, request):
#         try:
#             # Create a Mock Order object that mimics the Django Model
#             # This allows create_sales_quotation to work without a real DB record
#             mock_item = SimpleNamespace(
#                 item_code="FG0000145",
#                 qty=84,
#                 price_list_basic=1286
#             )
#            
#             order = SimpleNamespace(
#                 id="TEST-ORDER-001",
#                 card_code="CUSTA000486",
#                 created_at="2026-02-10",
#                 po_number="7801514523",
#                 ship_to_address="WAL MART INDIA PVT LTD LUDHIANA 4717",
#                 bill_to_address="WAL MART INDIA PVT LTD LUDHIANA 4717",
#                 dispatch_from_id=3,
#                 items=SimpleNamespace(all=lambda: [mock_item])
#             )
#
#             service = SyncService(triggered_by="manual_test")
#             result = service.create_sales_quotation(order)
#
#             return Response(result, status=status.HTTP_200_OK)
#
#         except Exception as e:
#             return Response(
#                 {"error": str(e)},
#                 status=status.HTTP_400_BAD_REQUEST
#             )
            
class GetPartyByCategoryView(APIView):
    def get(self , request):
        category = request.query_params.get('category')
        if not category:
            return Response({
                'success': False,
                'message': 'category query parameter is required'
            }, status=status.HTTP_400_BAD_REQUEST)
            
        parties = Party.objects.filter(category__iexact=category)
        serializer = PartySerializer(parties, many=True)
        
        return Response({
            'success': True,
            'data': serializer.data
        })




class   ApproveSalesOrderAPIView(APIView):
    """Approve an order and push to SAP"""
    
    def post(self, request):
        order_id = request.data.get('order_id')
    
        if not order_id:
            return Response({
                'success': False,
                'message': 'order_id is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            service = SyncService(triggered_by=request.user.username)
            order = Order.objects.get(id=order_id)

            if str(order.status).strip() == 'Completed':
                return Response({"error" : "Invoice Already posted"})

            result = service.create_sales_order(order)
            logger.info("Order %s approval result: %s", order_id, result)

            return Response({
                'success': True,
                'message': 'Order approved and pushed to SAP successfully',
                'data': result,
                'errors': None
            })
        except Order.DoesNotExist:
            return Response({
                'success': False,
                'message': 'Order not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                'success': False,
                'message': str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)