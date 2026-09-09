"""Who may sell what: user-to-party and party-to-product assignments.

Split out of `users/views.py` (plan item 3.3), and the bulk of it — twelve
views and twelve helpers, all concerned with the same question from different
directions (by user, by party, by product, in bulk).

These endpoints decide which customers a salesperson sees and at what rate,
so they are the ones the Phase 2 lockdown mattered most for: before it, several
answered unauthenticated requests.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.permissions import HasKey, IsAdminRole, effective_keys, is_admin
from users.models import User, UserPartyAssignment, PartyProductAssignment
from sap_sync.models import Party, Product, active_product_q
from decimal import Decimal
from django.db.models import Q
from ._shared import (
    _get_user_assignment_category,
    _normalize_category,
)


#: The key that governs the Party Assignment screen, for both reading someone
#: else's assignments and rewriting them.
PARTY_ASSIGNMENT_KEY = 'Party_Assignment'


def _party_assignment_permissions():
    """Gate for the Party Assignment screen: the key, not the admin role.

    `Party_Assignment` was already grantable from the Permissions page and from
    a role's bundle — but it only opened the PAGE, while every write behind it
    still demanded `IsAdminRole`. A role granted the page therefore got a screen
    it could read and not use, and because `Party_Assignment.tsx` catches every
    exception alike, the 403 surfaced to the user as "check your connection".

    Granting the key now carries the authority the grant implies. This is not a
    widening of who CAN be given the power — an administrator had to tick the
    box either way — it is the tick box finally meaning what it says.

    Still a real boundary: party assignment decides which customers a
    salesperson can see, so an ungranted user gets 403 exactly as before.
    """
    return [IsAuthenticated(), HasKey(PARTY_ASSIGNMENT_KEY)]


def _may_manage_party_assignments(user):
    """True for admins and for anyone holding the Party Assignment key."""
    return PARTY_ASSIGNMENT_KEY in effective_keys(user)


def _combo_free_defaults(data):
    """Pull the optional combo -> free-item mapping out of a request body.

    Only keys the caller actually sent are returned, so existing clients that
    know nothing about combos never blank an already-configured mapping.
    """
    defaults = {}

    if 'free_item_code' in data:
        free_item_code = str(data.get('free_item_code') or '').strip()
        defaults['free_item_code'] = free_item_code or None

    if 'free_qty_per_unit' in data:
        raw = data.get('free_qty_per_unit')
        if raw in (None, ''):
            defaults['free_qty_per_unit'] = None
        else:
            try:
                defaults['free_qty_per_unit'] = Decimal(str(raw))
            except (TypeError, ValueError, ArithmeticError):
                defaults['free_qty_per_unit'] = None

    return defaults


def _party_key(card_code, category):
    return f"{str(card_code or '').strip()}||{_normalize_category(category) or ''}"


def _normalize_party_selections(raw_selections=None, raw_card_codes=None):
    normalized = set()

    if isinstance(raw_selections, list):
        for selection in raw_selections:
            if not isinstance(selection, dict):
                continue
            card_code = str(selection.get('card_code') or '').strip()
            category = _normalize_category(selection.get('category'))
            if card_code:
                normalized.add((card_code, category))

    if normalized:
        return normalized

    for card_code in raw_card_codes or []:
        normalized_card_code = str(card_code or '').strip()
        if normalized_card_code:
            normalized.add((normalized_card_code, None))

    return normalized


def _active_product_exists(item_code, category):
    return Product.objects.filter(
        active_product_q(),
        item_code=item_code,
        category=category,
    ).exists()


def _get_party_for_assignment(card_code, category):
    queryset = Party.objects.filter(card_code=card_code)
    if category:
        party = queryset.filter(category__iexact=category).order_by('id').first()
        if party:
            return party
    return queryset.order_by('id').first()


def _get_user_assignment_categories(user):
    """All categories assigned to the user (normalized), falling back to the
    single primary `category` FK for users created before multi-category."""
    names = []
    seen = set()
    manager = getattr(user, 'categories', None)
    if manager is not None:
        try:
            for cat in manager.all():
                name = _normalize_category(getattr(cat, 'category', cat))
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        except Exception:
            names = []
    if names:
        return names
    primary = _get_user_assignment_category(user)
    return [primary] if primary else []


def _resolve_requested_category(user, requested):
    """Category to scope party assignment to. Honor a request-supplied category
    when it is one of the user's categories; otherwise use the primary."""
    requested = _normalize_category(requested)
    if requested and requested in _get_user_assignment_categories(user):
        return requested
    return _get_user_assignment_category(user)


def _get_category_filtered_assignments(queryset, user_category):
    if not user_category:
        return queryset
    return queryset.filter(
        Q(category=user_category) | Q(category__isnull=True) | Q(category='')
    )


def _serialize_user_party_assignments(assignments, preferred_category=None):
    parties_list = []
    card_codes = []
    seen_keys = set()

    for assignment in assignments:
        resolved_category = _normalize_category(assignment.category)
        if not resolved_category and preferred_category:
            resolved_category = _normalize_category(preferred_category)
        party = _get_party_for_assignment(assignment.card_code, resolved_category)
        if not party:
            continue

        normalized_category = _normalize_category(
            assignment.category or resolved_category or getattr(party, 'category', None)
        )
        key = _party_key(assignment.card_code, normalized_category)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        parties_list.append({
            'id': party.id,
            'card_code': party.card_code,
            'card_name': party.card_name,
            'state': party.state,
            'main_group': party.main_group,
            # Reflect the assignment's category (not the Party master's), so the
            # same card_code assigned under OIL and BEVERAGES stays distinct.
            'category': normalized_category or getattr(party, 'category', None),
            'assigned_at': assignment.assigned_at,
        })
        card_codes.append(assignment.card_code)

    return {
        'parties': parties_list,
        'card_codes': list(dict.fromkeys(card_codes)),
        'total_assigned': len(parties_list),
    }


# ---------------------------------------------------------------------------
# OpenAPI response shapes — documentation only, no runtime effect.
#
# `UserPartiesView` builds its body by hand and spreads
# `_serialize_user_party_assignments()` into it, so the three keys `parties`,
# `card_codes` and `total_assigned` sit as SIBLINGS of `user` inside `data`
# rather than nested under a key of their own. Declared to match that literally
# — the shape is only obvious if you read the helper.
# ---------------------------------------------------------------------------

#: One row of `data.parties`. `id` is the PARTY's primary key, not the
#: assignment's — the assignment itself has no id in this payload. The rest of
#: the fields come from the `Party` master except `category`, which is the
#: ASSIGNMENT's category (so the same card_code assigned under both OIL and
#: BEVERAGES stays two distinct rows), and `assigned_at`, which comes from
#: `UserPartyAssignment`. `state`, `main_group` and `category` are all nullable
#: on the model, so all three can come back null.
USER_PARTY_ITEM = inline_serializer(name='UserPartyAssignmentItem', fields={
    'id': serializers.IntegerField(),
    'card_code': serializers.CharField(),
    'card_name': serializers.CharField(),
    'state': serializers.CharField(allow_null=True),
    'main_group': serializers.CharField(allow_null=True),
    'category': serializers.CharField(allow_null=True),
    'assigned_at': serializers.DateTimeField(),
}, many=True)

#: `GET /api/auth/users/{user_id}/parties/` 200.
USER_PARTIES_RESPONSE = inline_serializer(name='UserParties', fields={
    'success': serializers.BooleanField(),
    'data': inline_serializer(name='UserPartiesData', fields={
        # A three-key summary of the user, NOT the full `UserSerializer`.
        'user': inline_serializer(name='UserPartiesUser', fields={
            'id': serializers.IntegerField(),
            'username': serializers.CharField(),
            'name': serializers.CharField(),
        }),
        'parties': USER_PARTY_ITEM,
        'card_codes': serializers.ListField(child=serializers.CharField()),
        'total_assigned': serializers.IntegerField(),
    }),
})

#: The 403 and 404 branches, which carry `message` and no `data` at all.
USER_PARTIES_ERROR_RESPONSE = inline_serializer(name='UserPartiesError', fields={
    'success': serializers.BooleanField(),
    'message': serializers.CharField(),
})


class PartyUsersView(APIView):
    """Which users are assigned to a party. Administrators only.

    Party assignment decides which customers a salesperson can see, so both
    reading and rewriting it belong to the assignment-management screen.
    """

    def get_permissions(self):
        return _party_assignment_permissions()

    def get(self, request, card_code):
        category = _normalize_category(request.query_params.get('category'))
        party = _get_party_for_assignment(card_code, category)
        if not party:
            return Response({'success': False, 'message': 'Party not found'}, status=status.HTTP_404_NOT_FOUND)

        assignments = UserPartyAssignment.objects.filter(card_code=card_code, is_active=True).select_related('user')
        if category:
            assignments = assignments.filter(category=category)
        users = [{
            'id': a.user.id,
            'username': a.user.username,
            'name': a.user.name,
            'role': a.user.role,
            'assigned_at': a.assigned_at,
            'category': a.category,
        } for a in assignments]

        return Response({
            'success': True,
            'data': {
                'party': {'card_code': party.card_code, 'card_name': party.card_name, 'category': party.category},
                'users': users, 'total_assigned': len(users)
            }
        })


# NOTE: a second, identical-in-name `UserPartiesView` used to be declared here.
# The one defined further down (with the `?category=all` branch the dashboard
# needs) shadowed it at import time, so this copy was dead code that never
# handled a request — while carrying the stricter permission that made the file
# look safer than it was. Deleted; the live class is the only one now.


class AssignPartiesView(APIView):
    """Assign parties to a user. Administrators only.

    Was `AllowAny`. Party assignment IS the data-visibility boundary for
    salespeople — an anonymous caller could grant themselves every customer in
    the company.
    """

    def get_permissions(self):
        return _party_assignment_permissions()

    def post(self, request):
        user_id = request.data.get('user_id')
        card_codes = request.data.get('card_codes', [])

        if not user_id:
            return Response({'success': False, 'message': 'user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        all_active_assignments = UserPartyAssignment.objects.filter(user=user, is_active=True)
        # Assign within the requested category (one of the user's categories);
        # falls back to the primary. Scoping existing rows to this category means
        # assigning for one category never disturbs parties in another.
        user_category = _resolve_requested_category(user, request.data.get('category'))
        relevant_existing_qs = _get_category_filtered_assignments(all_active_assignments, user_category)

        existing = {
            (assignment.card_code, _normalize_category(assignment.category))
            for assignment in relevant_existing_qs
        }
        new_assignments = {
            (str(card_code or '').strip(), user_category)
            for card_code in card_codes
            if str(card_code or '').strip()
        }
        to_add = new_assignments - existing
        to_remove = existing - new_assignments

        added_count = 0
        for card_code, category in to_add:
            party_queryset = Party.objects.filter(card_code=card_code)
            if category:
                party_queryset = party_queryset.filter(category__iexact=category)
            if party_queryset.exists():
                UserPartyAssignment.objects.update_or_create(
                    user=user,
                    card_code=card_code,
                    category=category,
                    defaults={'is_active': True, 'assigned_by': request.user}
                )
                added_count += 1

        removed_count = 0
        for card_code, category in to_remove:
            # Deactivate per-object (not a bulk .update()) so the audit signals
            # fire and each removal is logged with its is_active change, matching
            # the single-removal endpoint used by the app.
            for assignment in UserPartyAssignment.objects.filter(
                user=user,
                card_code=card_code,
                category=category,
                is_active=True,
            ):
                assignment.is_active = False
                assignment.save(update_fields=['is_active'])
                removed_count += 1

        return Response({
            'success': True,
            'message': f'Added: {added_count}, Removed: {removed_count}',
            'data': {'added': added_count, 'removed': removed_count, 'total_assigned': len(new_assignments)}
        })

class BulkAssignUsersPartiesView(APIView):
    """Bulk party assignment from an upload. Administrators only."""

    def get_permissions(self):
        return _party_assignment_permissions()

    def post(self, request):
        rows = request.data.get('rows', [])
        if not isinstance(rows, list) or not rows:
            return Response(
                {'success': False, 'message': 'rows must be a non-empty list'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        added_count = 0
        existing_count = 0
        errors = []

        for index, row in enumerate(rows):
            row_number = index + 2
            if not isinstance(row, dict):
                errors.append(f'Row {row_number}: invalid row')
                continue

            user_identifier = str(
                row.get('user_id')
                or row.get('username')
                or row.get('user_name')
                or row.get('name')
                or ''
            ).strip()
            card_code = str(row.get('card_code') or row.get('party_code') or '').strip()

            if not user_identifier or not card_code:
                errors.append(f'Row {row_number}: user and party code are required')
                continue

            user_queryset = User.objects.filter(is_active=True)
            if str(user_identifier).isdigit():
                user_queryset = user_queryset.filter(
                    Q(id=int(user_identifier)) |
                    Q(username__iexact=user_identifier) |
                    Q(name__iexact=user_identifier)
                )
            else:
                user_queryset = user_queryset.filter(
                    Q(username__iexact=user_identifier) |
                    Q(name__iexact=user_identifier)
                )
            user = user_queryset.first()
            if not user:
                errors.append(f'Row {row_number}: user {user_identifier} not found')
                continue

            user_category = _get_user_assignment_category(user)
            if not user_category:
                errors.append(f'Row {row_number}: user {user.username} has no category')
                continue

            party = Party.objects.filter(
                card_code=card_code,
                category__iexact=user_category,
            ).order_by('id').first()
            if not party:
                errors.append(f'Row {row_number}: party {card_code} not found in {user_category}')
                continue
            user_category = _normalize_category(party.category)

            _assignment, created = UserPartyAssignment.objects.update_or_create(
                user=user,
                card_code=card_code,
                category=user_category,
                defaults={
                    'is_active': True,
                    'assigned_by': request.user if request.user.is_authenticated else None,
                },
            )
            if created:
                added_count += 1
            else:
                existing_count += 1

        return Response({
            'success': len(errors) == 0,
            'message': f'Added: {added_count}, Existing/updated: {existing_count}, Errors: {len(errors)}',
            'data': {
                'added': added_count,
                'existing': existing_count,
                'errors': errors,
                'total_rows': len(rows),
            },
        })

class PartyProductsView(APIView):
    """Get all products assigned to a party with their basic_rate"""
    permission_classes = [IsAuthenticated]

    def get(self, request, card_code):
        party = Party.objects.filter(card_code=card_code).first()
        if not party:
            return Response({'success': False, 'message': 'Party not found'}, status=status.HTTP_404_NOT_FOUND)

        category_filter = request.query_params.get('category', None)

        active_products = Product.objects.filter(active_product_q()).values_list('item_code', flat=True)
        assignments = PartyProductAssignment.objects.filter(
            card_code=card_code,
            is_active=True,
            item_code__in=active_products,
        )
        if category_filter:
            assignments = assignments.filter(category=category_filter)

        products_list = []
        for a in assignments:
            product = Product.objects.filter(active_product_q(), item_code=a.item_code, category=a.category).first()
            if product:
                products_list.append({
                    'id': product.id,
                    'item_code': product.item_code,
                    'item_name': product.item_name,
                    'category': product.category,
                    'brand': product.brand,
                    'variety': product.variety,
                    'sub_group': product.sub_group,
                    'sal_pack_unit': product.sal_pack_unit,
                    'basic_rate': float(a.basic_rate),
                    'free_item_code': a.free_item_code,
                    'free_qty_per_unit': (
                        float(a.free_qty_per_unit) if a.free_qty_per_unit is not None else None
                    ),
                    'assigned_at': a.assigned_at,
                })

        return Response({
            'success': True,
            'data': {
                'party': {
                    'card_code': party.card_code,
                    'card_name': party.card_name,
                    'state': party.state,
                    'main_group': party.main_group,
                },
                'products': products_list,
                'total_assigned': len(products_list)
            }
        })

class AssignProductToPartyView(APIView):
    """
    Add single product to party with basic_rate
    Body: {"card_code": "C001", "item_code": "FG001", "category": "OIL", "basic_rate": 150.50}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        card_code = request.data.get('card_code')
        item_code = request.data.get('item_code')
        category = request.data.get('category')
        basic_rate = request.data.get('basic_rate', 0)

        if not all([card_code, item_code, category]):
            return Response({
                'success': False,
                'message': 'card_code, item_code and category are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not Party.objects.filter(card_code=card_code).exists():
            return Response({'success': False, 'message': 'Party not found'}, status=status.HTTP_404_NOT_FOUND)

        if not _active_product_exists(item_code, category):
            return Response({'success': False, 'message': 'Product not found or inactive'}, status=status.HTTP_404_NOT_FOUND)

        obj, created = PartyProductAssignment.objects.update_or_create(
            card_code=card_code,
            item_code=item_code,
            category=category,
            defaults={
                'basic_rate': Decimal(str(basic_rate)),
                'is_active': True,
                'assigned_by': request.user,
                **_combo_free_defaults(request.data),
            }
        )

        return Response({
            'success': True,
            'message': 'Product added' if created else 'Product updated',
            'data': {
                'item_code': item_code,
                'category': category,
                'basic_rate': float(obj.basic_rate),
                'free_item_code': obj.free_item_code,
                'free_qty_per_unit': (
                    float(obj.free_qty_per_unit) if obj.free_qty_per_unit is not None else None
                ),
            }
        })
class BulkAssignPartyToProductView(APIView):
    """
    Assign multiple products to multiple parties
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        party_selections = request.data.get('party_selections', [])
        card_codes = request.data.get('card_codes', [])
        products = request.data.get('products', [])

        normalized_party_selections = _normalize_party_selections(party_selections, card_codes)

        if not normalized_party_selections:
            return Response({'success': False, 'message': 'party selection is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not products:
            return Response({'success': False, 'message': 'products is required'}, status=status.HTTP_400_BAD_REQUEST)

        added = 0
        updated = 0
        errors = []

        for card_code, party_category in normalized_party_selections:
            party_queryset = Party.objects.filter(card_code=card_code)
            if party_category:
                party_queryset = party_queryset.filter(category__iexact=party_category)
            if not party_queryset.exists():
                category_label = f"|{party_category}" if party_category else ""
                errors.append(f"{card_code}{category_label}: Party not found")
                continue

            for prod in products:
                item_code = prod.get('item_code')
                category = _normalize_category(prod.get('category'))
                basic_rate = prod.get('basic_rate', 0)

                if not item_code or not category:
                    errors.append(f"{card_code}: Missing item_code/category")
                    continue

                if party_category and category != party_category:
                    continue

                # Validate product exists
                if not _active_product_exists(item_code, category):
                    errors.append(f"{card_code}: {item_code}|{category} not found or inactive")
                    continue

                obj, created = PartyProductAssignment.objects.update_or_create(
                    card_code=card_code,
                    item_code=item_code,
                    category=category,
                    defaults={
                        'basic_rate': Decimal(str(basic_rate)),
                        'is_active': True,
                        'assigned_by': request.user
                    }
                )

                if created:
                    added += 1
                else:
                    updated += 1

        return Response({
            'success': True,
            'message': f'Added: {added}, Updated: {updated}',
            'data': {
                'added': added,
                'updated': updated,
                'errors': errors
            }
        }, status=status.HTTP_200_OK)

class BulkAssignProductsToPartyView(APIView):
    """
    Add multiple products to a party
    Body: {
        "card_code": "C001",
        "products": [
            {"item_code": "FG001", "category": "OIL", "basic_rate": 150.50},
            {"item_code": "FG002", "category": "BEVERAGES", "basic_rate": 120.00}
        ]
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        card_code = request.data.get('card_code')
        products = request.data.get('products', [])

        if not card_code:
            return Response({'success': False, 'message': 'card_code is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not Party.objects.filter(card_code=card_code).exists():
            return Response({'success': False, 'message': 'Party not found'}, status=status.HTTP_404_NOT_FOUND)

        added = 0
        updated = 0
        errors = []

        for prod in products:
            item_code = prod.get('item_code')
            category = prod.get('category')
            basic_rate = prod.get('basic_rate', 0)

            if not item_code or not category:
                errors.append(f"Missing item_code or category")
                continue

            if not _active_product_exists(item_code, category):
                errors.append(f"Product {item_code}|{category} not found or inactive")
                continue

            obj, created = PartyProductAssignment.objects.update_or_create(
                card_code=card_code,
                item_code=item_code,
                category=category,
                defaults={
                    'basic_rate': Decimal(str(basic_rate)),
                    'is_active': True,
                    'assigned_by': request.user
                }
            )
            if created:
                added += 1
            else:
                updated += 1

        return Response({
            'success': True,
            'message': f'Added: {added}, Updated: {updated}',
            'data': {'added': added, 'updated': updated, 'errors': errors}
        })

class UpdateProductRateView(APIView):
    """Update basic_rate for a party-product"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        card_code = request.data.get('card_code')
        item_code = request.data.get('item_code')
        category = request.data.get('category')
        basic_rate = request.data.get('basic_rate')

        if not all([card_code, item_code, category]) or basic_rate is None:
            return Response({
                'success': False,
                'message': 'card_code, item_code, category and basic_rate are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            assignment = PartyProductAssignment.objects.get(
                card_code=card_code, item_code=item_code, category=category, is_active=True
            )
            assignment.basic_rate = Decimal(str(basic_rate))
            for field, value in _combo_free_defaults(request.data).items():
                setattr(assignment, field, value)
            assignment.save()

            return Response({
                'success': True,
                'message': 'Rate updated',
                'data': {
                    'basic_rate': float(assignment.basic_rate),
                    'free_item_code': assignment.free_item_code,
                    'free_qty_per_unit': (
                        float(assignment.free_qty_per_unit)
                        if assignment.free_qty_per_unit is not None
                        else None
                    ),
                }
            })
        except PartyProductAssignment.DoesNotExist:
            return Response({'success': False, 'message': 'Assignment not found'}, status=status.HTTP_404_NOT_FOUND)

# A "+" in the SAP name is what marks a combo pack, but it also catches bundles
# that are not 1+1 combos and must stay off the Combo Mapping page:
#   * "... COMBO 10 SET"   -- multi-set cartons
#   * "... 3 PCS SHRINKED" -- shrink-wrapped multipacks
# Matched case-insensitively as substrings. 'SHRINK' rather than 'SHRINKED' on
# purpose: one item is named "... SHRINKED 1 PCS" and another just "SHRINK".
COMBO_NAME_EXCLUDE_TERMS = ('COMBO', 'SHRINK')


class ComboMappingsView(APIView):
    """Combo packs and the free-of-cost item each one carries.

    A combo is any assigned product whose SAP name contains a "+", minus the
    bundles listed in COMBO_NAME_EXCLUDE_TERMS above. The mapping
    itself lives on `party_product_assignments`, so this view collapses the
    per-party rows into one entry per item_code/category and writes a change
    back to every one of them at once — combos give away the same product for
    every party.
    """
    permission_classes = [IsAuthenticated]

    def _product_payload(self, item_code, category):
        if not item_code:
            return None
        product = (
            Product.objects.filter(active_product_q(), item_code=item_code, category=category).first()
            or Product.objects.filter(active_product_q(), item_code=item_code).first()
        )
        if not product:
            # Mapped to something SAP no longer lists — surface the raw code so
            # the page can show it as broken rather than silently as "unmapped".
            return {'item_code': item_code, 'item_name': None, 'sal_factor2': None}
        return {
            'item_code': product.item_code,
            'item_name': product.item_name,
            'sal_factor2': product.sal_factor2,
        }

    def get(self, request):
        combo_candidates = Product.objects.filter(active_product_q(), item_name__contains='+')
        for term in COMBO_NAME_EXCLUDE_TERMS:
            combo_candidates = combo_candidates.exclude(item_name__icontains=term)
        combo_products = {
            (p.item_code, p.category): p
            for p in combo_candidates
        }
        if not combo_products:
            return Response({'success': True, 'data': {'combos': []}})

        assignments = PartyProductAssignment.objects.filter(
            is_active=True,
            item_code__in={code for code, _ in combo_products},
        )

        grouped = {}
        for assignment in assignments:
            key = (assignment.item_code, assignment.category)
            product = combo_products.get(key)
            if not product:
                continue

            entry = grouped.setdefault(key, {
                'item_code': assignment.item_code,
                'item_name': product.item_name,
                'category': assignment.category,
                'sal_factor2': product.sal_factor2,
                'party_count': 0,
                'mapped_party_count': 0,
                'parent_item_code': None,
                'free_item_code': None,
                'free_qty_per_unit': None,
            })
            entry['party_count'] += 1

            parent_item_code = (assignment.parent_item_code or '').strip()
            if parent_item_code and not entry['parent_item_code']:
                entry['parent_item_code'] = parent_item_code

            free_item_code = (assignment.free_item_code or '').strip()
            # Both halves are required: the split needs a parent to price and a
            # free item to give away, so a half-filled row is not "mapped".
            if parent_item_code and free_item_code:
                entry['mapped_party_count'] += 1
            if free_item_code:
                if not entry['free_item_code']:
                    entry['free_item_code'] = free_item_code
                    entry['free_qty_per_unit'] = (
                        float(assignment.free_qty_per_unit)
                        if assignment.free_qty_per_unit is not None
                        else None
                    )

        combos = []
        for entry in grouped.values():
            entry['parent_item'] = self._product_payload(entry['parent_item_code'], entry['category'])
            entry['free_item'] = self._product_payload(entry['free_item_code'], entry['category'])
            # Flags a combo whose parties disagree, e.g. after a partial update.
            entry['is_partially_mapped'] = (
                0 < entry['mapped_party_count'] < entry['party_count']
            )
            combos.append(entry)

        combos.sort(key=lambda c: (c['item_name'] or '', c['category']))
        return Response({'success': True, 'data': {'combos': combos}})

    def post(self, request):
        item_code = str(request.data.get('item_code') or '').strip()
        category = _normalize_category(request.data.get('category'))
        parent_item_code = str(request.data.get('parent_item_code') or '').strip()
        free_item_code = str(request.data.get('free_item_code') or '').strip()
        raw_qty = request.data.get('free_qty_per_unit')

        if not item_code or not category:
            return Response({
                'success': False,
                'message': 'item_code and category are required',
            }, status=status.HTTP_400_BAD_REQUEST)

        if free_item_code and not _active_product_exists(free_item_code, category):
            # The free half often sits in a different category to the combo, so
            # fall back to "exists anywhere" before rejecting it.
            if not Product.objects.filter(active_product_q(), item_code=free_item_code).exists():
                return Response({
                    'success': False,
                    'message': f'Free item {free_item_code} not found or inactive',
                }, status=status.HTTP_404_NOT_FOUND)

        if parent_item_code and not _active_product_exists(parent_item_code, category):
            if not Product.objects.filter(active_product_q(), item_code=parent_item_code).exists():
                return Response({
                    'success': False,
                    'message': f'Parent item {parent_item_code} not found or inactive',
                }, status=status.HTTP_404_NOT_FOUND)

        if free_item_code and free_item_code == item_code:
            return Response({
                'success': False,
                'message': 'A combo cannot give away itself',
            }, status=status.HTTP_400_BAD_REQUEST)

        if parent_item_code and parent_item_code == item_code:
            return Response({
                'success': False,
                'message': 'A combo cannot be its own parent item',
            }, status=status.HTTP_400_BAD_REQUEST)

        if parent_item_code and parent_item_code == free_item_code:
            return Response({
                'success': False,
                'message': 'The parent and free item must be different products',
            }, status=status.HTTP_400_BAD_REQUEST)

        # Ordering splits a mapped combo into both halves, so half a mapping
        # would produce a priced line with no giveaway (or the reverse). Save
        # both or clear both.
        if bool(parent_item_code) != bool(free_item_code):
            missing = 'parent_item_code' if not parent_item_code else 'free_item_code'
            return Response({
                'success': False,
                'message': f'Both halves are required to map a combo; {missing} is missing',
            }, status=status.HTTP_400_BAD_REQUEST)

        free_qty_per_unit = None
        if raw_qty not in (None, ''):
            try:
                free_qty_per_unit = Decimal(str(raw_qty))
            except (TypeError, ValueError, ArithmeticError):
                return Response({
                    'success': False,
                    'message': 'free_qty_per_unit must be a number',
                }, status=status.HTTP_400_BAD_REQUEST)
            if free_qty_per_unit <= 0:
                return Response({
                    'success': False,
                    'message': 'free_qty_per_unit must be greater than 0',
                }, status=status.HTTP_400_BAD_REQUEST)

        updated = PartyProductAssignment.objects.filter(
            item_code=item_code, category=category, is_active=True,
        ).update(
            parent_item_code=parent_item_code or None,
            free_item_code=free_item_code or None,
            free_qty_per_unit=free_qty_per_unit,
        )

        if not updated:
            return Response({
                'success': False,
                'message': 'No active party assignments for this combo',
            }, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'success': True,
            'message': (
                f'Mapping cleared for {updated} parties' if not free_item_code
                else f'Mapping saved for {updated} parties'
            ),
            'data': {
                'item_code': item_code,
                'category': category,
                'parent_item_code': parent_item_code or None,
                'free_item_code': free_item_code or None,
                'free_qty_per_unit': float(free_qty_per_unit) if free_qty_per_unit is not None else None,
                'party_count': updated,
            },
        })


class RemoveProductFromPartyView(APIView):
    """Remove a product from party"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        card_code = request.data.get('card_code')
        item_code = request.data.get('item_code')
        category = request.data.get('category')

        if not all([card_code, item_code, category]):
            return Response({
                'success': False,
                'message': 'card_code, item_code and category are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            assignment = PartyProductAssignment.objects.get(
                card_code=card_code, item_code=item_code, category=category, is_active=True
            )
            assignment.is_active = False
            assignment.save()
            return Response({'success': True, 'message': 'Product removed from party'})
        except PartyProductAssignment.DoesNotExist:
            return Response({'success': False, 'message': 'Assignment not found'}, status=status.HTTP_404_NOT_FOUND)

class RemovePartyAssignmentView(APIView):
    """Revoke a party assignment. Administrators only."""

    def get_permissions(self):
        return _party_assignment_permissions()

    def post(self, request):
        user_id = request.data.get('user_id')
        card_code = request.data.get('card_code')
        category = _normalize_category(request.data.get('category'))

        if not user_id or not card_code:
            return Response({'success': False, 'message': 'user_id and card_code are required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            assignment_queryset = UserPartyAssignment.objects.filter(user_id=user_id, card_code=card_code, is_active=True)
            if category:
                assignment_queryset = assignment_queryset.filter(category=category)
            assignment = assignment_queryset.get()
            assignment.is_active = False
            assignment.save()
            return Response({'success': True, 'message': 'Party removed from user'})
        except UserPartyAssignment.DoesNotExist:
            return Response({'success': False, 'message': 'Assignment not found'}, status=status.HTTP_404_NOT_FOUND)
    
@extend_schema(
    parameters=[
        OpenApiParameter(
            name='category',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description='Scope the assignments to one category. The literal '
                        'value `all` bypasses scoping and returns every '
                        'category. Any other value is honoured only if it is '
                        "one of the user's own categories, otherwise the "
                        "user's primary category is used.",
        ),
    ],
    responses={
        200: USER_PARTIES_RESPONSE,
        403: USER_PARTIES_ERROR_RESPONSE,
        404: USER_PARTIES_ERROR_RESPONSE,
    },
    description='Parties assigned to `user_id`. 403 when a non-admin asks for '
                "someone else's record and 404 when the user does not exist; "
                'both carry `{success: false, message}` and no `data`.',
)
class UserPartiesView(APIView):
    """The parties assigned to a user — own record, or any record for an admin.

    Was `AllowAny`, which exposed the whole customer-to-salesperson map to
    anonymous callers. Scoped rather than admin-gated because the dashboard
    calls this for the logged-in user on every load; only the assignment-
    management screen reads someone else's.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        # Reading someone else's record is the assignment screen's job, so it
        # follows the same key as the writes — otherwise a user granted
        # Party_Assignment could open the page and save, but not see which
        # parties were already ticked.
        if user_id != request.user.pk and not _may_manage_party_assignments(request.user):
            return Response(
                {'success': False,
                 'message': 'You may only view your own party assignments'},
                status=status.HTTP_403_FORBIDDEN)

        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        assignments = UserPartyAssignment.objects.filter(user=user, is_active=True).order_by('-assigned_at')
        requested_category = request.query_params.get('category')
        # The dashboard needs every assigned party across all of the user's
        # categories (e.g. the same card_code under both OIL and BEVERAGES). Pass
        # ?category=all to bypass the single-category scoping used by the
        # assignment-management screen.
        if str(requested_category or '').strip().lower() == 'all':
            serialized = _serialize_user_party_assignments(assignments)
        else:
            user_category = _resolve_requested_category(user, requested_category)
            assignments = _get_category_filtered_assignments(assignments, user_category)
            serialized = _serialize_user_party_assignments(assignments, preferred_category=user_category)

        return Response({
            'success': True,
            'data': {
                'user': {'id': user.id, 'username': user.username, 'name': user.name},
                **serialized,
            }
        })
