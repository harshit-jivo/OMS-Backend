"""Master-data views: parties, addresses, products, dispatch locations.

Fifth domain out of `orders/views.py` (plan item 3.1). These are the read
endpoints the order form is built from — who can be sold to, what can be sold,
and where it ships from — as opposed to anything that moves an order through
its flow.

"Masters" is the codebase's own word for these: `sap_sync` calls the same
tables the product and party masters, and they are mirrored from SAP rather
than owned here.

Nothing left behind depends on any of this, and it depends on nothing left
behind — the closure came back empty in both directions.
"""
from urllib import request
import re
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_serializer,
    inline_serializer,
)
from sap_sync.models import Branch
from orders.serializers import DispatchLocationSerializer, BranchSerializer, PartyAddressSerializer, ProductSerializer, StaffProductSerializer
from orders.models import PartyProductAssignment, DispatchLocation, UserPartyAssignment, ProductDetails, Order, StaffProductPrice
from users.views._shared import _get_user_assignment_categories
from rest_framework.generics import ListAPIView
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q

from core.permissions import HasAnyKey, HasKey
from sap_sync.models import Party as SapParty, PartyAddress as SapPartyAddress, Product as SapProduct, active_product_q


# ---------------------------------------------------------------------------
# OpenAPI response shapes (`@extend_schema` below). Documentation only.
#
# Three of these endpoints assemble their rows by hand, so there is no
# serializer for drf-spectacular to infer from and the generated document
# describes no body at all. The shapes below were read off the view bodies.
#
# A note on the number types. These views put model values straight into a
# plain dict, so DRF's JSON encoder — not a serializer field — decides how they
# render, and it turns a `Decimal` into a JSON NUMBER. That is why `basic_rate`
# and `tax_rate` are declared as floats here even though a ModelSerializer
# would have sent them as strings.
# ---------------------------------------------------------------------------

# `orders.serializers` and `sap_sync.serializers` each define a
# `ProductSerializer`, a `PartyAddressSerializer` and a `BranchSerializer` — on
# DIFFERENT models, with different field sets. drf-spectacular names a schema
# component after the serializer CLASS, so naming the orders ones in a response
# would overwrite sap_sync's identically-named components and hand
# `/api/sap/products/`, `/api/sap/parties/.../addresses/` and
# `/api/sap/branches/` a body they do not return.
#
# These three subclasses exist only to carry a distinct component name. They
# add no field and are never instantiated at runtime — the views below still
# return the originals, whose fields these inherit, so the documented shape
# cannot drift from the real one.


@extend_schema_serializer(component_name='OrdersProduct')
class _OrdersProductSchema(ProductSerializer):
    """Schema alias for `orders.serializers.ProductSerializer`."""


@extend_schema_serializer(component_name='OrdersPartyAddress')
class _OrdersPartyAddressSchema(PartyAddressSerializer):
    """Schema alias for `orders.serializers.PartyAddressSerializer`."""


@extend_schema_serializer(component_name='OrdersBranch')
class _OrdersBranchSchema(BranchSerializer):
    """Schema alias for `orders.serializers.BranchSerializer`."""


#: The free-of-cost companion line of a combo pack. Null when the combo has no
#: mapping yet, which is the normal state for an unmapped combo.
_PARTY_PRODUCT_FREE_ITEM = inline_serializer(
    name='PartyProductFreeItem',
    fields={
        'item_code': serializers.CharField(),
        'item_name': serializers.CharField(allow_null=True),
        'category': serializers.CharField(),
        'brand': serializers.CharField(allow_null=True),
        # `variety` and `sub_group` are BOTH the product's sub_group — the Add
        # Sales cascade reads `variety`, everything else reads `sub_group`.
        'variety': serializers.CharField(allow_null=True),
        'sub_group': serializers.CharField(allow_null=True),
        'sal_factor2': serializers.FloatField(allow_null=True),
        'sal_pack_unit': serializers.CharField(allow_null=True),
        'tax_rate': serializers.FloatField(allow_null=True),
        # Always the literal 0: the auto-added free line is never priced.
        'basic_rate': serializers.FloatField(),
    },
    allow_null=True,
)

PARTY_PRODUCT_ROWS = inline_serializer(
    name='PartyProductRow',
    fields={
        'item_code': serializers.CharField(),
        'category': serializers.CharField(),
        # `PartyProductAssignment.basic_rate` (Decimal) rendered as a number.
        'basic_rate': serializers.FloatField(),
        # `assignment.updated_at.isoformat()`, or null.
        'updated_at': serializers.DateTimeField(allow_null=True),
        'item_name': serializers.CharField(allow_null=True),
        'sal_factor2': serializers.FloatField(allow_null=True),
        'tax_rate': serializers.FloatField(allow_null=True),
        'sal_pack_unit': serializers.CharField(allow_null=True),
        'brand': serializers.CharField(allow_null=True),
        # Duplicates, both fed from the product's sub_group. See above.
        'variety': serializers.CharField(allow_null=True),
        'sub_group': serializers.CharField(allow_null=True),
        'combo_scheme_id': serializers.IntegerField(allow_null=True),
        'combo_scheme_name': serializers.CharField(allow_null=True),
        'is_combo': serializers.BooleanField(),
        # These three are null together: they are only filled for a combo pack
        # that has a free-half mapping.
        'free_item_code': serializers.CharField(allow_null=True),
        'free_qty_per_unit': serializers.FloatField(allow_null=True),
        'free_item': _PARTY_PRODUCT_FREE_ITEM,
    },
    many=True,
)

#: `PartyView` — a picker option. `value`/`label` duplicate
#: `card_code`/`card_name`; both pairs are sent because different screens read
#: different halves.
PARTY_OPTIONS = inline_serializer(
    name='PartyOption',
    fields={
        'value': serializers.CharField(),
        'card_code': serializers.CharField(),
        'card_name': serializers.CharField(),
        # f"{card_name} ({card_code})"
        'label': serializers.CharField(),
        'category': serializers.CharField(allow_null=True),
        'state': serializers.CharField(allow_null=True),
    },
    many=True,
)

PARTY_ADDRESSES_RESPONSE = inline_serializer(
    name='PartyAddresses',
    fields={
        'bill_to': _OrdersPartyAddressSchema(many=True),
        'ship_to': _OrdersPartyAddressSchema(many=True),
        # Always False. The fallback branch that would set it is commented out
        # in the view; the key is still sent, so it is declared.
        'is_fallback': serializers.BooleanField(),
    },
)

PARTY_ADDRESSES_ERROR = inline_serializer(
    name='PartyAddressesError',
    fields={'error': serializers.CharField()},
)


def _active_sap_item_codes():
    return SapProduct.objects.filter(active_product_q()).values_list('item_code', flat=True)

def extract_type_from_name(item_name):
    """Extract size/type like '1 LTR', '500 ML', '5 KG' from item name"""
    if not item_name:
        return None

    # More flexible pattern - handles various formats
    # Matches: 1 LTR, 1LTR, 1 Ltr, 1L, 500 ML, 500ML, 5 KG, 5KG, 200 GM, 200GM, etc.
    pattern = r'(\d+(?:\.\d+)?\s*(?:LTR|LITRE|LITER|L|ML|KG|KGS|GM|GMS|GRAM|G|PCS|PC|POUCH|TIN|JAR|BTL|BTL|CAN|BOTTLE|PACK|PKT|BOX)S?)\b'

    match = re.search(pattern, item_name.upper())

    if match:
        # Normalize the result (e.g., "1LTR" -> "1 LTR")
        result = match.group(1).strip()
        # Add space between number and unit if missing
        result = re.sub(r'(\d)([A-Z])', r'\1 \2', result)
        return result
    return None

def is_combo_item_name(item_name):
    """Combo packs are named "<paid part> + <free part>" in SAP."""
    return '+' in str(item_name or '')


# One free unit per combo unit: the order form counts pieces, and a combo pack
# carries one of the free product per piece. The trailing "4 PCS" / "6 PCS" in a
# combo name is the paid SKU's carton config (it equals sal_factor2), not a free
# count, so it is deliberately not parsed. Set `free_qty_per_unit` on the
# assignment for the rare pack that gives away more than one.
DEFAULT_COMBO_FREE_QTY_PER_UNIT = 1.0


def _resolve_combo_free_mapping(assignment):
    """Return (free_item_code, free_qty_per_unit) for a combo assignment.

    The mapping lives on `party_product_assignments`, so it is nominally
    per-party. A combo's free half is the same product for everyone, though, so
    a blank mapping falls back to any other party's row for the same
    item_code/category — set it once and every party picks it up.
    """
    free_item_code = (assignment.free_item_code or '').strip()
    free_qty = assignment.free_qty_per_unit

    if not free_item_code:
        donor = PartyProductAssignment.objects.filter(
            item_code=assignment.item_code,
            category=assignment.category,
            is_active=True,
        ).exclude(free_item_code__isnull=True).exclude(free_item_code='').first()
        if donor:
            free_item_code = (donor.free_item_code or '').strip()
            if free_qty is None:
                free_qty = donor.free_qty_per_unit

    if not free_item_code:
        return None, None

    qty_per_unit = float(free_qty) if free_qty is not None else DEFAULT_COMBO_FREE_QTY_PER_UNIT
    return free_item_code, (qty_per_unit if qty_per_unit > 0 else DEFAULT_COMBO_FREE_QTY_PER_UNIT)


def _serialize_free_product(free_item_code, category):
    product = (
        SapProduct.objects.filter(active_product_q(), item_code=free_item_code, category=category).first()
        or SapProduct.objects.filter(active_product_q(), item_code=free_item_code).first()
    )
    if not product:
        return None
    return {
        'item_code': product.item_code,
        'item_name': product.item_name,
        'category': product.category,
        'brand': product.brand,
        'variety': product.sub_group,
        'sub_group': product.sub_group,
        'sal_factor2': product.sal_factor2,
        'sal_pack_unit': product.sal_pack_unit,
        'tax_rate': product.tax_rate,
        # Free of cost — the auto-added order line is always zero-priced.
        'basic_rate': 0,
    }


@extend_schema(
    responses={200: PARTY_PRODUCT_ROWS},
    description='The products one party may be sold, with that party\'s rate '
                '— the product cascade on the Add Sales screen. A bare array. '
                'An assignment whose item_code has no active SAP product is '
                'skipped, so this can be shorter than the assignment list and '
                'can legitimately be empty.',
)
class PartyProductsView(APIView):

    def get(self, request, card_code):
        normalized_card_code = (card_code or '').strip()
        active_item_codes = _active_sap_item_codes()
        assignments = list(
            PartyProductAssignment.objects.filter(
                card_code=normalized_card_code,
                is_active=True,
                item_code__in=active_item_codes,
            ).select_related('scheme').order_by('category', 'item_code')
        )

        # Phase 4.2 query audit: this used to run one (sometimes two) SapProduct
        # lookups PER assignment -- a real N+1 that scaled with the party's
        # catalogue size. One batched query for every item_code the assignments
        # could possibly need, keyed both by (item_code, category) and by
        # item_code alone, replaces that -- see
        # docs/CODEBASE_AND_REFACTOR_PLAN.md Phase 4.2.
        item_codes = {assignment.item_code for assignment in assignments}
        products_by_item_and_category = {}
        products_by_item_code = {}
        for product in SapProduct.objects.filter(active_product_q(), item_code__in=item_codes):
            products_by_item_and_category.setdefault(
                (product.item_code, product.category), product)
            # Some older assignment rows can carry a valid item_code with a
            # category that no longer matches SAP metadata exactly -- fall back
            # to any active product for that item_code, same as before.
            products_by_item_code.setdefault(product.item_code, product)

        rows = []
        for assignment in assignments:
            product = products_by_item_and_category.get(
                (assignment.item_code, assignment.category)
            ) or products_by_item_code.get(assignment.item_code)

            if not product:
                continue

            item_name = getattr(product, 'item_name', None)
            is_combo = is_combo_item_name(item_name)
            free_item_code, free_qty_per_unit = (
                _resolve_combo_free_mapping(assignment) if is_combo else (None, None)
            )
            free_product = (
                _serialize_free_product(free_item_code, assignment.category) if free_item_code else None
            )

            rows.append({
                'item_code': assignment.item_code,
                'category': assignment.category,
                'basic_rate': assignment.basic_rate,
                # When this party-product assignment was last updated. The
                # Distributor page uses it to block ordering products whose
                # rate/assignment wasn't refreshed in the current month.
                'updated_at': assignment.updated_at.isoformat() if assignment.updated_at else None,
                'item_name': getattr(product, 'item_name', None),
                'sal_factor2': getattr(product, 'sal_factor2', None),
                'tax_rate': getattr(product, 'tax_rate', None),
                'sal_pack_unit': getattr(product, 'sal_pack_unit', None),
                'brand': getattr(product, 'brand', None),
                # The Add Sales cascade's "Sub Group" column reads `variety`;
                # feed it the product's sub_group so the column groups by sub group.
                'variety': getattr(product, 'sub_group', None),
                'sub_group': getattr(product, 'sub_group', None),
                'combo_scheme_id': assignment.scheme_id,
                'combo_scheme_name': assignment.scheme.scheme_name if assignment.scheme else None,
                # Combo pack -> free-of-cost companion line. `free_item` is null
                # when the combo has no mapping yet, and the UI then behaves as
                # it always did.
                'is_combo': is_combo,
                'free_item_code': free_product['item_code'] if free_product else None,
                'free_qty_per_unit': free_qty_per_unit if free_product else None,
                'free_item': free_product,
            })

        return Response(rows)

# class PartyProductsView(APIView):
#     permission_classes = [AllowAny]
#     def get(self, request, card_code):
#         from django.db import connection
#         cursor = connection.cursor()
#         cursor.execute("""
#             SELECT ppa.item_code, ppa.category, ppa.basic_rate,
#                    p.item_name, p.sal_factor2, p.tax_rate, p.sal_pack_unit,
#                    p.brand, p.variety
#             FROM party_product_assignments ppa
#             LEFT JOIN sap_products p ON ppa.item_code = p.item_code
#             WHERE ppa.card_code = %s AND ppa.is_active = true
#             ORDER BY ppa.category, p.item_name
#         """, [card_code])
    
#         columns = [col[0] for col in cursor.description]
#         rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
#         return Response(rows)
    
@extend_schema(
    responses={200: PARTY_OPTIONS},
    description='The parties assigned to the calling user, as picker options. '
                'A bare array, ordered by card_name, and scoped to '
                '`request.user` — it takes no user parameter.',
)
class PartyView(APIView):

    def get(self, request):
        user_id = request.user.id

        assignments = UserPartyAssignment.objects.filter(
            user_id=user_id,
            is_active=True
        ).values('card_code', 'category').distinct()

        # Scope to the categories this user actually works in.
        #
        # An assignment can outlive the account's category: a salesperson moved
        # from Oil to Beverages keeps whatever was assigned to them under Oil,
        # and those parties then sit in the picker offering nothing usable —
        # their products, rates and addresses are all category-scoped, so an
        # order against one cannot be priced. Showing them is worse than
        # hiding them, because the failure only appears two steps later.
        #
        # An empty list means "no category set", and that is deliberately NOT
        # treated as "nothing": admins and auditors are set up without one, and
        # scoping them to nothing would empty the picker for the people who most
        # need to see everything. Same posture as `invoice.branches_for_user`.
        user_categories = _get_user_assignment_categories(request.user)

        assignment_filters = Q()
        fallback_card_codes = []
        for assignment in assignments:
            card_code = assignment.get('card_code')
            category = str(assignment.get('category') or '').strip()
            if not card_code:
                continue
            if user_categories and category and category.upper() not in user_categories:
                continue
            if category:
                assignment_filters |= Q(card_code=card_code, category__iexact=category)
            else:
                fallback_card_codes.append(card_code)

        parties = SapParty.objects.none()
        if assignment_filters:
            parties = parties | SapParty.objects.filter(assignment_filters)
        if fallback_card_codes:
            parties = parties | SapParty.objects.filter(card_code__in=fallback_card_codes)
        parties = parties.distinct().order_by('card_name')

        data = [
            {
                'value': p.card_code,
                'card_code': p.card_code,
                'card_name': p.card_name,
                'label': f"{p.card_name} ({p.card_code})",
                'category': p.category,
                'state': p.state,
            }
            for p in parties
        ]

        return Response(data)

class DispatchLocationListView(ListAPIView):
    serializer_class = DispatchLocationSerializer
    queryset = DispatchLocation.objects.filter(is_active=True, name__icontains='FACTORY').order_by('name')

@extend_schema(
    responses={
        200: PARTY_ADDRESSES_RESPONSE,
        400: OpenApiResponse(
            response=PARTY_ADDRESSES_ERROR,
            description='`card_code` was missing from the query string. The '
                        'view returns this itself, so the body is `{"error": '
                        '...}` and nothing else.',
        ),
    },
    description='Bill-to and ship-to addresses for one party, split by '
                '`address_type` and optionally narrowed by `category`. Either '
                'list can be empty; `is_fallback` is always False.',
)
class PartyAddressesView(APIView):


    def get(self, request):
        card_code = request.query_params.get('card_code')
        category = str(request.query_params.get('category') or '').strip().upper()
        
        if not card_code:
            return Response({'error': 'card_code is required'}, status=400)

        bill_addresses = SapPartyAddress.objects.filter(
            card_code=card_code,
            address_type='B'
        )

        ship_addresses = SapPartyAddress.objects.filter(
            card_code=card_code,
            address_type='S'
        )

        if category:
            bill_addresses = bill_addresses.filter(category__iexact=category)
            ship_addresses = ship_addresses.filter(category__iexact=category)

        # if not bill_addresses.exists() and not ship_addresses.exists():
        #     try:
        #         party = SapPartyAddress.objects.filter(card_code=card_code)
        #         fallback_address = {
        #             'id': 0,
        #             # 'address_id': party.card_name if party else '',
        #             'full_address': party.address if party else '',
        #             'gst_number': None
        #         }
        #         return Response({
        #             'bill_to': [fallback_address],
        #             'ship_to': [fallback_address],
        #             'is_fallback': True
        #         })
        #     except SapPartyAddress.DoesNotExist:
        #         return Response({
        #             'bill_to': [],
        #             'ship_to': [],
        #             'is_fallback': False
        #         })

        bill_data = PartyAddressSerializer(bill_addresses, many=True).data
        ship_data = PartyAddressSerializer(ship_addresses, many=True).data

        # if not bill_data:
        #     try:
        #         party = SapPartyAddress.objects.filter(card_code=card_code).first()
        #         bill_data = [{
        #             'id': 0,
        #             # 'address_id': party.card_name,
        #             'full_address': party.address,
        #             'gst_number': None
        #         }]
        #     except Parties.DoesNotExist:
        #         pass
                
        # if not ship_data:
        #     try:
        #         party = SapPartyAddress.objects.get(card_code=card_code)
        #         ship_data = [{
        #             'id': 0,
        #             # 'address_id': party.card_name,
        #             'full_address': party.address,
        #             'gst_number': None
        #         }]
        #     except SapPartyAddress.DoesNotExist:
        #         pass

        return Response({
            'bill_to': bill_data,
            'ship_to': ship_data,
            'is_fallback': False
        })

class ProductFiltersView(APIView):

    def get(self, request):
        category = request.query_params.get('category')
        brand = request.query_params.get('brand')
        variety = request.query_params.get('variety')
        active_item_codes = _active_sap_item_codes()

        # Always get all categories
        categories = ProductDetails.objects.filter(
            item_code__in=active_item_codes
        ).exclude(
            category__isnull=True
        ).exclude(
            category=''
        ).values_list('category', flat=True).distinct().order_by('category')

        # Get brands - only if category is provided
        brands = []
        if category:
            brands = ProductDetails.objects.filter(
                item_code__in=active_item_codes,
                category=category
            ).exclude(
                brand__isnull=True
            ).exclude(
                brand=''
            ).values_list('brand', flat=True).distinct().order_by('brand')

        # Get varieties - only if category AND brand are provided
        varieties = []
        if category and brand:
            varieties = ProductDetails.objects.filter(
                item_code__in=active_item_codes,
                category=category,
                brand=brand
            ).exclude(
                variety__isnull=True
            ).exclude(
                variety=''
            ).values_list('variety', flat=True).distinct().order_by('variety')
    
        # Get types - ONLY if category, brand, AND variety are provided
        types = []
        if category and brand and variety:
            types_query = ProductDetails.objects.filter(
                item_code__in=active_item_codes,
                category=category,
                brand=brand,
                variety=variety
            )
        
            item_names = types_query.values_list('item_name', flat=True)
            types_set = set()
            has_others = False
        
            for name in item_names:
                item_type = extract_type_from_name(name)
                if item_type:
                    types_set.add(item_type)
                else:
                    has_others = True
        
            types = sorted(list(types_set))
        
            if has_others:
                types.append('Others')

        return Response({
            'categories': [{'label': c, 'value': c} for c in categories],
            'brands': [{'label': b, 'value': b} for b in brands],
            'varieties': [{'label': v, 'value': v} for v in varieties],
            'types': [{'label': t, 'value': t} for t in types]
        })

@extend_schema(
    responses={200: _OrdersProductSchema(many=True)},
    description='The product catalogue for the order form\'s picker, limited '
                'to item codes still active in SAP and optionally narrowed by '
                'the `category`, `brand`, `variety` and `type` query '
                'parameters. A bare array — the serializer was always there, '
                'spectacular simply cannot see it through a bare APIView.',
)
class ProductListView(APIView):

    def get(self, request):
        category = request.query_params.get('category')
        brand = request.query_params.get('brand')
        variety = request.query_params.get('variety')
        item_type = request.query_params.get('type')

        products = ProductDetails.objects.filter(item_code__in=_active_sap_item_codes())

        if category:
            products = products.filter(category=category)
        if brand:
            products = products.filter(brand=brand)
        if variety:
            products = products.filter(variety=variety)
        if item_type:
            products = products.filter(item_name__icontains=item_type)

        serializer = ProductSerializer(products.order_by('item_name'), many=True)
        return Response(serializer.data)



@extend_schema(
    responses={200: _OrdersBranchSchema(many=True)},
    description='Factory dispatch locations for the "dispatch from" selector. '
                'A bare array. Note `bpl_id` is a STRING here even though the '
                'column is an integer — see `orders.serializers.'
                'BranchSerializer` for why that is deliberate.',
)
class BranchView(APIView):
    """Factory dispatch locations, for the "dispatch from" selector.

    Reads `sap_sync.Branch` — the model the SAP sync writes and the one that
    matches the table. It used to read `orders.Branches`, a second unmanaged
    model on the same table that declared every column wrongly.

    `distinct('bpl_name')` is deliberate and Postgres-specific (DISTINCT ON):
    the table is unique on (bpl_id, category), so one physical factory appears
    once per company DB it exists in, and the selector wants it once.
    """

    def get(self, request):
        branches = (Branch.objects
                    .filter(bpl_name__icontains='FACTORY')
                    .order_by("bpl_name")
                    .distinct('bpl_name'))
        serializer = BranchSerializer(branches, many=True)
        return Response(serializer.data)

class StaffProductsAPIView(APIView):
    """The staff product catalogue, and the rates attached to it.

    Gated per METHOD, because the two are different authorities:

    * GET lists which products staff may order and at what rate. Both the
      Staff Orders screen and the Staff Rate Assignment screen need it, so
      either key admits.
    * POST sets those rates and un-assigns products. That is the Staff Rate
      Assignment screen alone.

    This view previously declared no ``permission_classes``, so it fell back
    to the project default of ``IsAuthenticated`` — meaning any signed-in user
    could read the staff price list AND overwrite it, while the pages in front
    of it were admin-only. Hiding a route is not access control; this is the
    server half that was missing.
    """

    def get_permissions(self):
        if self.request.method == 'POST':
            return [IsAuthenticated(), HasKey('Staff_Rate_Assignment')]
        return [IsAuthenticated(), HasAnyKey('Staff', 'Staff_Rate_Assignment')]

    def get(self, request):
        products = SapProduct.objects.filter(active_product_q(), staff_prices__isnull=False).distinct()

        serializer = StaffProductSerializer(products, many=True)

        return Response(serializer.data)

    def post(self, request):
        products = request.data.get("products", [])
        removed_products = request.data.get("removed_products", [])
        if not isinstance(products, list):
            return Response(
                {"error": "products must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(removed_products, list):
            return Response(
                {"error": "removed_products must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not products and not removed_products:
            return Response(
                {"error": "products or removed_products must be a non-empty list"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        saved = []
        removed = []
        errors = []

        for index, item in enumerate(removed_products):
            product_id = item.get("product_id") or item.get("id")
            item_code = item.get("item_code")
            category = str(item.get("category") or "").strip()

            if not category:
                errors.append({"index": index, "error": "category is required"})
                continue

            product = None
            if product_id:
                product = SapProduct.objects.filter(
                    id=product_id,
                    category__iexact=category,
                ).first()
            if not product and item_code:
                product = SapProduct.objects.filter(
                    item_code=item_code,
                    category__iexact=category,
                ).first()

            if not product:
                errors.append({"index": index, "error": "product not found"})
                continue

            deleted_count, _ = StaffProductPrice.objects.filter(product=product).delete()
            if deleted_count:
                removed.append({
                    "product_id": product.id,
                    "item_code": product.item_code,
                    "item_name": product.item_name,
                    "category": product.category,
                })

        for index, item in enumerate(products):
            product_id = item.get("product_id") or item.get("id")
            item_code = item.get("item_code")
            category = str(item.get("category") or "").strip()
            rate = item.get("rate")

            if not category:
                errors.append({"index": index, "error": "category is required"})
                continue

            if rate in (None, ""):
                errors.append({"index": index, "error": "rate is required"})
                continue

            try:
                rate_value = float(rate)
            except (TypeError, ValueError):
                errors.append({"index": index, "error": "rate must be a number"})
                continue

            if rate_value < 0:
                errors.append({"index": index, "error": "rate cannot be negative"})
                continue

            product = None
            if product_id:
                product = SapProduct.objects.filter(
                    active_product_q(),
                    id=product_id,
                    category__iexact=category,
                ).first()
            if not product and item_code:
                product = SapProduct.objects.filter(
                    active_product_q(),
                    item_code=item_code,
                    category__iexact=category,
                ).first()

            if not product:
                errors.append({"index": index, "error": "product not found or inactive"})
                continue

            staff_price = StaffProductPrice.objects.filter(product=product).first()
            if staff_price:
                staff_price.rate = rate_value
                staff_price.save(update_fields=["rate"])
            else:
                staff_price = StaffProductPrice.objects.create(
                    product=product,
                    rate=rate_value,
                )

            saved.append({
                "id": staff_price.id,
                "product_id": product.id,
                "item_code": product.item_code,
                "item_name": product.item_name,
                "category": product.category,
                "rate": str(staff_price.rate),
            })

        if errors:
            return Response(
                {"saved": saved, "removed": removed, "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message": "Staff rates saved successfully",
                "saved": saved,
                "removed": removed,
            },
            status=status.HTTP_200_OK,
        )

class UserPartyView(APIView):
    
    def get(self , request):
        user = request.query_params.get("user")
        if not user:
            return Response({"error": "user parameter is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        parties = (
            Order.objects.filter(created_by__id=user)
            .exclude(card_code__isnull=True)
            .exclude(card_code__exact="")
            .values("card_code", "card_name")
            .distinct()
        )
