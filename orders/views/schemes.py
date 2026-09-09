"""Scheme administration views.

Third domain out of `orders/views.py` (plan item 3.1), and the cleanest cut so
far: these ten views depend on NO other top-level name in the module, and
nothing left behind depends on them.

Note what did NOT come with them. `_extract_order_item_schemes`,
`_apply_engine_schemes` and `_scheme_entry` stay in `_legacy` because they
belong to order CREATION — deciding which free goods a line earns as an order
is placed — not to administering scheme definitions. The two read the same
tables and are easy to confuse; the dependency graph is what separates them.

Covers both scheme generations: the original `Scheme`/`SchemeAssignment` views
and the v2 views that replaced them.
"""
from urllib import request
from drf_spectacular.utils import extend_schema, inline_serializer
from orders.serializers import SchemeProductSerializer, SchemeWriteSerializer
from orders.models import OrderItemScheme
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import serializers
from rest_framework import status
from django.db.models import Q
from sap_sync.models import Product as SapProduct, active_product_q
from orders.services import scheme_engine
from users.models import SchemeProduct, State
from django.db import transaction as _db_transaction
from orders.models import Scheme, SchemeAssignment
from orders.serializers import SchemeV2Serializer, SchemeAssignmentSerializer




#: `SchemeListView`'s row. Documentation only.
#:
#: The first four keys come from a `.values()` call, so they are the raw
#: `users.SchemeProduct` columns. `item_name` is NOT one of them: it is grafted
#: on in Python afterwards from a second query, which is exactly why no
#: serializer can describe this body.
SCHEME_LIST_ROWS = inline_serializer(
    name='SchemeListRow',
    fields={
        'scheme_id': serializers.IntegerField(),
        'scheme_name': serializers.CharField(),
        'state_code': serializers.CharField(allow_null=True),
        'item_code': serializers.CharField(allow_null=True),
        # Grafted on: the SAP product name, falling back to the item_code and
        # then to ''. Always a string, never null.
        'item_name': serializers.CharField(allow_blank=True),
    },
    many=True,
)


@extend_schema(
    responses={200: SCHEME_LIST_ROWS},
    description='Active scheme products, optionally filtered by the '
                '`state_code` query parameter (matched against either the '
                'state code or its name). A bare array; `item_name` is added '
                'in Python after the query, so it is present on every row even '
                'though it is not a column.',
)
class SchemeListView(APIView):

    def get(self, request):
        from users.models import SchemeProduct
        queryset = SchemeProduct.objects.filter(is_active=True)
        state_code = (request.query_params.get('state_code') or '').strip()

        if state_code:
            state_match = State.objects.filter(
                Q(code__iexact=state_code) | Q(name__iexact=state_code),
                is_active=True,
            ).values('code', 'name').first()
            state_values = {state_code}
            if state_match:
                state_values.update(
                    value for value in (state_match.get('code'), state_match.get('name')) if value
                )
            state_filter = Q()
            for value in state_values:
                state_filter |= Q(state_code__iexact=value)
            queryset = queryset.filter(state_filter)

        schemes = list(
            queryset
            .order_by('scheme_name', 'scheme_id', 'state_code')
            .values('scheme_id', 'scheme_name', 'state_code', 'item_code')
        )

        # Add Sales renders the giveaway as its own line in the item list, so the
        # picker has to name the item, not just the offer. One query for the whole
        # page rather than one per scheme.
        item_codes = {s['item_code'] for s in schemes if s.get('item_code')}
        names = {}
        if item_codes:
            names = dict(
                SapProduct.objects
                .filter(active_product_q(), item_code__in=item_codes)
                .values_list('item_code', 'item_name')
            )
        for scheme in schemes:
            scheme['item_name'] = names.get(scheme.get('item_code')) or scheme.get('item_code') or ''

        return Response(schemes)

class SchemeProductView(APIView):

    def get(self, request):
        queryset = SchemeProduct.objects.select_related('state').filter(is_active=True)

        product_id = request.query_params.get('product_id')
        item_code = request.query_params.get('item_code')
        scheme_id = request.query_params.get('scheme_id')
        scheme_name = request.query_params.get('scheme_name')

        if scheme_id:
            queryset = queryset.filter(scheme_id=scheme_id)
        if scheme_name:
            queryset = queryset.filter(scheme_name=scheme_name)
        if product_id:
            product = SapProduct.objects.filter(id=product_id).only('item_code').first()
            queryset = queryset.filter(item_code=product.item_code) if product else queryset.none()
        if item_code:
            queryset = queryset.filter(item_code=item_code)

        serializer = SchemeProductSerializer(queryset.order_by('scheme_name', 'scheme_id'), many=True)
        return Response({
            'success': True,
            'data': serializer.data,
            'total': len(serializer.data),
        })

class CreateSchemeView(APIView):

    def post(self, request):
        serializer = SchemeWriteSerializer(data=request.data)

        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Scheme created successfully',
                'data': serializer.data
            }, status=status.HTTP_201_CREATED)

        return Response({
            'success': False,
            'message': 'Failed to create scheme',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)


class SchemeManageListView(APIView):
    """Full scheme rows for the Add Scheme management table.

    `SchemeListView` (/orders/schemes/) is deliberately left alone — it feeds the
    Add Sales picker and returns only scheme_id/scheme_name/state_code. Managing
    schemes needs item_code and is_active as well.
    """


    def get(self, request):
        queryset = SchemeProduct.objects.all()

        include_inactive = str(
            request.query_params.get('include_inactive') or ''
        ).strip().lower() in {'1', 'true', 'yes'}
        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        state_code = (request.query_params.get('state_code') or '').strip()
        if state_code:
            queryset = queryset.filter(state_code__iexact=state_code)

        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(scheme_name__icontains=search) | Q(item_code__icontains=search)
            )

        serializer = SchemeProductSerializer(
            queryset.order_by('scheme_name', 'state_code', 'scheme_id'), many=True
        )
        return Response({
            'success': True,
            'data': serializer.data,
            'total': len(serializer.data),
        })


class SchemeDetailView(APIView):
    """Read / update / delete a single scheme."""


    def _get_object(self, scheme_id):
        return SchemeProduct.objects.filter(scheme_id=scheme_id).first()

    def get(self, request, scheme_id):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'data': SchemeProductSerializer(scheme).data})

    def put(self, request, scheme_id):
        return self._update(request, scheme_id, partial=False)

    def patch(self, request, scheme_id):
        return self._update(request, scheme_id, partial=True)

    def _update(self, request, scheme_id, partial):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        serializer = SchemeWriteSerializer(scheme, data=request.data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Scheme updated successfully',
                'data': SchemeProductSerializer(scheme).data,
            })

        return Response({
            'success': False,
            'message': 'Failed to update scheme',
            'errors': serializer.errors,
        }, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, scheme_id):
        """Deactivate by default.

        OrderItem.scheme and OrderItemScheme.scheme are FKs with on_delete=SET_NULL,
        so a row deletion would blank the scheme on every historical order that used
        it — losing the record of what was given away. Every read path already
        filters is_active=True (the pickers, and the SAP free-line fan-out), so
        deactivating removes the scheme everywhere it matters and stays reversible.
        Pass ?hard=true to actually delete the row.
        """
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        hard = str(request.query_params.get('hard') or '').strip().lower() in {'1', 'true', 'yes'}

        used_by_orders = OrderItemScheme.objects.filter(scheme_id=scheme_id).count()

        if hard:
            if used_by_orders:
                return Response({
                    'success': False,
                    'message': (
                        f'Cannot hard-delete: {used_by_orders} order line(s) reference '
                        'this scheme. Deactivate it instead.'
                    ),
                }, status=status.HTTP_409_CONFLICT)
            scheme.delete()
            return Response({'success': True, 'message': 'Scheme deleted', 'deactivated': False})

        scheme.is_active = False
        scheme.save(update_fields=['is_active'])
        return Response({
            'success': True,
            'message': 'Scheme deactivated',
            'deactivated': True,
            'used_by_order_lines': used_by_orders,
        })


class SchemeV2ListCreateView(APIView):

    def get(self, request):
        queryset = Scheme.objects.prefetch_related('benefits', 'triggers', 'assignments')

        include_inactive = str(
            request.query_params.get('include_inactive') or ''
        ).strip().lower() in {'1', 'true', 'yes'}
        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(Q(code__icontains=search) | Q(name__icontains=search))

        # ?category=OIL keeps the uncategorised ("every category") schemes too —
        # they apply to OIL as much as to anything else.
        category = (request.query_params.get('category') or '').strip()
        if category:
            queryset = queryset.filter(Q(category='') | Q(category__iexact=category))

        # Filter by who a scheme reaches, e.g. ?scope_type=STATE&scope_value=PB
        scope_type = (request.query_params.get('scope_type') or '').strip()
        scope_value = (request.query_params.get('scope_value') or '').strip()
        if scope_type:
            scope_q = Q(assignments__scope_type=scope_type, assignments__is_active=True)
            if scope_value:
                scope_q &= Q(assignments__scope_value__iexact=scope_value)
            queryset = queryset.filter(scope_q).distinct()

        serializer = SchemeV2Serializer(queryset, many=True)
        return Response({'success': True, 'data': serializer.data, 'total': len(serializer.data)})

    def post(self, request):
        serializer = SchemeV2Serializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({'success': False, 'message': 'Failed to create scheme',
                             'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        with _db_transaction.atomic():
            serializer.save()
        return Response({'success': True, 'message': 'Scheme created', 'data': serializer.data},
                        status=status.HTTP_201_CREATED)


class SchemeV2DetailView(APIView):

    def _get_object(self, scheme_id):
        return (
            Scheme.objects
            .prefetch_related('benefits', 'triggers', 'assignments')
            .filter(pk=scheme_id)
            .first()
        )

    def get(self, request, scheme_id):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'data': SchemeV2Serializer(scheme).data})

    def patch(self, request, scheme_id):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        serializer = SchemeV2Serializer(scheme, data=request.data, partial=True,
                                        context={'request': request})
        if not serializer.is_valid():
            return Response({'success': False, 'message': 'Failed to update scheme',
                             'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        with _db_transaction.atomic():
            serializer.save()
        return Response({'success': True, 'message': 'Scheme updated', 'data': serializer.data})

    def delete(self, request, scheme_id):
        """Deactivate by default.

        OrderItemScheme.scheme_v2 is PROTECT, so a scheme referenced by any order
        line cannot be deleted at all -- the giveaway record has to survive.
        Deactivating removes it from every read path and stays reversible.
        """
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        hard = str(request.query_params.get('hard') or '').strip().lower() in {'1', 'true', 'yes'}
        used_by_orders = OrderItemScheme.objects.filter(scheme_v2_id=scheme_id).count()

        if hard:
            if used_by_orders:
                return Response({
                    'success': False,
                    'message': (f'Cannot hard-delete: {used_by_orders} order line(s) reference '
                                'this scheme. Deactivate it instead.'),
                }, status=status.HTTP_409_CONFLICT)
            scheme.delete()
            return Response({'success': True, 'message': 'Scheme deleted', 'deactivated': False})

        scheme.is_active = False
        scheme.save(update_fields=['is_active', 'updated_at'])
        return Response({'success': True, 'message': 'Scheme deactivated', 'deactivated': True,
                         'used_by_order_lines': used_by_orders})


class SchemeAssignmentView(APIView):
    """Target a scheme at a party, a state, a main group, a category, or everyone.

    One STATE row reaches every vendor in that state -- including ones onboarded
    later -- which is the whole point of separating assignment from the offer.
    """


    def get(self, request, scheme_id):
        if not Scheme.objects.filter(pk=scheme_id).exists():
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        rows = SchemeAssignment.objects.filter(scheme_id=scheme_id).select_related('scheme')
        return Response({'success': True, 'data': SchemeAssignmentSerializer(rows, many=True).data})

    def post(self, request, scheme_id):
        scheme = Scheme.objects.filter(pk=scheme_id).first()
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        # Accept a single object or a list, so "assign to these 40 parties" is one call.
        payload = request.data if isinstance(request.data, list) else [request.data]
        serializer = SchemeAssignmentSerializer(data=payload, many=True)
        if not serializer.is_valid():
            return Response({'success': False, 'message': 'Failed to assign scheme',
                             'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user if getattr(request.user, 'is_authenticated', False) else None
        saved = []
        with _db_transaction.atomic():
            for row in serializer.validated_data:
                row.pop('scheme', None)
                obj, _created = SchemeAssignment.objects.update_or_create(
                    scheme=scheme,
                    scope_type=row['scope_type'],
                    scope_value=row.get('scope_value', ''),
                    category=row.get('category', ''),
                    defaults={
                        'is_exclusion': row.get('is_exclusion', False),
                        'valid_from': row.get('valid_from'),
                        'valid_to': row.get('valid_to'),
                        'is_active': row.get('is_active', True),
                        'created_by': user,
                    },
                )
                saved.append(obj)

        return Response({'success': True, 'message': f'{len(saved)} assignment(s) saved',
                         'data': SchemeAssignmentSerializer(saved, many=True).data},
                        status=status.HTTP_201_CREATED)

    def delete(self, request, scheme_id):
        assignment_id = request.query_params.get('assignment_id')
        if not assignment_id:
            return Response({'success': False, 'message': 'assignment_id is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        deleted, _ = SchemeAssignment.objects.filter(scheme_id=scheme_id, pk=assignment_id).delete()
        if not deleted:
            return Response({'success': False, 'message': 'Assignment not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'message': 'Assignment removed'})


class SchemePreviewView(APIView):
    """Dry-run the engine over a draft order.

    Body: {card_code, category, lines: [{item_code, category, sub_group, brand,
    qty, pcs, boxes, ltrs, is_auto_free, combo_source_code, item_type}, ...]}

    This is what makes state-wide targeting usable -- the salesperson never picks
    a scheme from a dropdown, the engine proposes and they confirm.
    """


    def post(self, request):
        card_code = (request.data.get('card_code') or '').strip()
        if not card_code:
            return Response({'success': False, 'message': 'card_code is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        lines = request.data.get('lines') or []
        if not isinstance(lines, list):
            return Response({'success': False, 'message': 'lines must be a list'},
                            status=status.HTTP_400_BAD_REQUEST)

        category = (request.data.get('category') or '').strip()
        ctx = scheme_engine.build_party_context(card_code, category)
        # Strict category rule for the order flow: a scheme is proposed only when
        # party category == product category == scheme category, all present.
        proposals = scheme_engine.resolve_schemes(
            card_code, category, lines, ctx=ctx, strict_category=True,
        )

        return Response({
            'success': True,
            'context': {
                'card_code': ctx.card_code,
                'category': ctx.category,
                'state_code': ctx.state_code,
                'main_group': ctx.main_group,
            },
            'proposals': [p.as_dict() for p in proposals],
        })


class SchemeApplicableView(APIView):
    """Everything reaching a vendor, with the scope that let each scheme in --
    the first question anyone asks about an unexpected giveaway."""


    def get(self, request):
        card_code = (request.query_params.get('card_code') or '').strip()
        if not card_code:
            return Response({'success': False, 'message': 'card_code is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        category = (request.query_params.get('category') or '').strip()
        rows = scheme_engine.applicable_schemes(card_code, category)
        return Response({'success': True, 'data': rows, 'total': len(rows)})
