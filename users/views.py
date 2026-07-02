from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from .serializers import LoginSerializer, UpdateUserSerializer, UserSerializer,StateSerializer, CompanySerializer,MainGroupSerializer,CreateUserSerializer, CategorySerializer
from rest_framework.generics import ListAPIView
from .models import State, Company, MainGroup,UserRole,User, UserPartyAssignment,PartyProductAssignment
from sap_sync.models import Party, Product, active_product_q
from orders.models import Categories, RateApproverRule
from decimal import Decimal
from django.db.models import Q


def _normalize_category(value):
    normalized = str(value or '').strip().upper()
    return normalized or None


def _selected_csv_names(value):
    return list(dict.fromkeys(
        name.strip()
        for name in str(value or '').split(',')
        if name.strip()
    ))


def _sync_rate_approver_rules(user):
    RateApproverRule.objects.filter(approver=user).delete()

    role_name = str(getattr(getattr(user, 'role', None), 'name', '') or '').strip().lower()
    category = _get_user_assignment_category(user)
    sub_groups = _selected_csv_names(getattr(user, 'sub_group', ''))

    if role_name != 'approver' or not category or not sub_groups:
        return

    for sub_group in sub_groups:
        RateApproverRule.objects.update_or_create(
            category=category,
            sub_group=sub_group,
            defaults={
                'approver': user,
                'is_active': True,
            },
        )


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


def _get_user_assignment_category(user):
    category_obj = getattr(user, 'category', None)
    return _normalize_category(getattr(category_obj, 'category', None))


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
            'category': getattr(party, 'category', None) or normalized_category,
            'assigned_at': assignment.assigned_at,
        })
        card_codes.append(assignment.card_code)

    return {
        'parties': parties_list,
        'card_codes': list(dict.fromkeys(card_codes)),
        'total_assigned': len(parties_list),
    }

class PartyUsersView(APIView):
    permission_classes = [AllowAny]

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
    
class RoleListView(APIView):
    def get(self, request):
        roles = UserRole.objects.filter(is_active=True).values('id', 'name', 'display_name')
        return Response(list(roles))

class UserPartiesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        assignments = UserPartyAssignment.objects.filter(user=user, is_active=True).order_by('-assigned_at')
        user_category = _get_user_assignment_category(user)
        assignments = _get_category_filtered_assignments(assignments, user_category)
        serialized = _serialize_user_party_assignments(assignments, preferred_category=user_category)

        return Response({
            'success': True,
            'data': {
                'user': {'id': user.id, 'username': user.username, 'name': user.name},
                **serialized,
            }
        })

class AssignPartiesView(APIView):
    permission_classes = [AllowAny]

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
        user_category = _get_user_assignment_category(user)
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
    permission_classes = [AllowAny]

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
                'assigned_by': request.user
            }
        )

        return Response({
            'success': True,
            'message': 'Product added' if created else 'Product updated',
            'data': {
                'item_code': item_code,
                'category': category,
                'basic_rate': float(obj.basic_rate)
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
            assignment.save()

            return Response({
                'success': True,
                'message': 'Rate updated',
                'data': {'basic_rate': float(assignment.basic_rate)}
            })
        except PartyProductAssignment.DoesNotExist:
            return Response({'success': False, 'message': 'Assignment not found'}, status=status.HTTP_404_NOT_FOUND)

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
    permission_classes = [AllowAny]

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
    
class UserPartiesView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        assignments = UserPartyAssignment.objects.filter(user=user, is_active=True).order_by('-assigned_at')
        user_category = _get_user_assignment_category(user)
        assignments = _get_category_filtered_assignments(assignments, user_category)
        serialized = _serialize_user_party_assignments(assignments, preferred_category=user_category)

        return Response({
            'success': True,
            'data': {
                'user': {'id': user.id, 'username': user.username, 'name': user.name},
                **serialized,
            }
        })

class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        
        if serializer.is_valid():
            user = serializer.validated_data['user']
            refresh = RefreshToken.for_user(user)
                
            return Response({
                'success': True,
                'message': 'Login successful',
                'data': {
                    'user': UserSerializer(user).data,
                    'tokens': {
                        'access': str(refresh.access_token),
                        'refresh': str(refresh),
                    }
                }
            })

        return Response({
            'success': False,
            'message': 'Login failed',
            'errors': serializer.errors
        }, status=status.HTTP_401_UNAUTHORIZED)
    
class UserListForAssignmentView(APIView):
    permission_classes = [AllowAny]

   
    def get(self, request):
        users = (
            User.objects.filter(is_active=True)
            .select_related('role', 'company', 'main_group', 'state')
            .prefetch_related('main_groups', 'user_states__state')
            .order_by('id')
        )
        data = UserSerializer(users, many=True).data
        return Response({'success': True, 'data': data})

class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            'success': True,
            'data': UserSerializer(request.user).data
        })


class PagePermissionsView(APIView):
    """Admin-managed per-user page access (list of page keys)."""
    permission_classes = [IsAuthenticated]

    def _is_admin(self, request):
        role = getattr(request.user, 'role', None)
        return bool(role and str(getattr(role, 'name', '')).strip().lower() == 'admin')

    def get(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({
            'success': True,
            'data': {'user_id': user.id, 'extra_pages': user.extra_pages or []},
        })

    def put(self, request, user_id):
        if not self._is_admin(request):
            return Response({'success': False, 'message': 'Only admin can change page permissions'},
                            status=status.HTTP_403_FORBIDDEN)
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'},
                            status=status.HTTP_404_NOT_FOUND)

        pages = request.data.get('extra_pages', [])
        if not isinstance(pages, list):
            return Response({'success': False, 'message': 'extra_pages must be a list'},
                            status=status.HTTP_400_BAD_REQUEST)

        cleaned = []
        for page in pages:
            page = str(page).strip()
            if page and page not in cleaned:
                cleaned.append(page)

        user.extra_pages = cleaned
        user.save(update_fields=['extra_pages', 'updated_at'])
        return Response({
            'success': True,
            'message': 'Page permissions updated',
            'data': {'user_id': user.id, 'extra_pages': cleaned},
        })

class StateListView(ListAPIView):
    """Get all active states"""
    permission_classes = [AllowAny]  # Or [IsAuthenticated] if login required
    serializer_class = StateSerializer
    queryset = State.objects.filter(is_active=True).order_by('name')

class CompanyListView(ListAPIView):
    """Get all active companies"""
    permission_classes = [AllowAny]
    serializer_class = CompanySerializer
    queryset = Company.objects.filter(is_active=True).order_by('name')
    
class MainGroupListView(ListAPIView):
    """Get all active main groups"""
    permission_classes = [AllowAny]
    serializer_class = MainGroupSerializer
    queryset = MainGroup.objects.filter(is_active=True).order_by('name')
    
class CategoryListView(ListAPIView):
    """Get all categories"""
    permission_classes = [AllowAny]
    serializer_class = CategorySerializer
    queryset = Categories.objects.all().order_by('category')

#Creating User
class CreateUserView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CreateUserSerializer(data=request.data)

        if serializer.is_valid():
            user = serializer.save()
            _sync_rate_approver_rules(user)
            return Response({
                'success': True,
                'message': 'User created successfully',
                'data': UserSerializer(user).data
            }, status=status.HTTP_201_CREATED)

        return Response({
            'success': False,
            'message': 'Failed to create user',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    permission_classes = [AllowAny]


class UserDetailView(APIView):
    permission_classes = [AllowAny] 
    
    def get(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
            serializer = UserSerializer(user)
            return Response({
                'success': True,
                'data': serializer.data
            }, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({
                'success': False,
                'message': 'User not found'
            }, status=status.HTTP_404_NOT_FOUND)

    def put(self, request, user_id):
 
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return Response({
                'success': False,
          'message': 'User not found'
            }, status=status.HTTP_404_NOT_FOUND)

        serializer = UpdateUserSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            updated_user = serializer.save()
            _sync_rate_approver_rules(updated_user)
            return Response({
 
                'success': True,
                'message': 'User updated successfully',
                'data': UserSerializer(updated_user).data
            }, status=status.HTTP_200_OK)

        return Response({
            'success': False,
            'message': 'Failed to update user',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
    
class DeleteUserView(APIView):

    permission_classes = [AllowAny]

    def post(self, request, user_id):
        try:
            user = User.objects.get(pk=user_id)
            user.is_active = False
            user.save()
            return Response({'success': True, 'message': 'User removed successfully'}, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({'success': False, 'message': 'User not found'}, status=status.HTTP_404_NOT_FOUND)
