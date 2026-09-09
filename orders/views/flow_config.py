"""Configuring the order flow: which stages an order passes through.

Split out of `orders/views.py` (plan item 3.1). These two views EDIT the flow
configuration; `orders.services.order_flow` is what reads it to decide an
order's next status. Keeping them apart matters because the rules are
consulted on every transition while the configuration is changed rarely and
only by an administrator — `_can_manage_order_flow` is the guard.
"""
from urllib import request
from orders.models import PartyOrderFlowConfig
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Q
from sap_sync.models import Party as SapParty
from orders.services.order_flow import (
    ORDER_FLOW_TYPE_ASM,
    ORDER_FLOW_TYPE_CHOICES,
    _get_order_flow_config,
    _normalize_category,
    _normalize_order_flow_type,
)


RATE_CONDITION_CHOICES = {
    'BASIC_GT_MARKET': 'Price List (Basic) > Basic Price and Basic Price != 0',
    'BASIC_LT_MARKET': 'Price List (Basic) < Basic Price',
    'BASIC_EQ_MARKET': 'Price List (Basic) = Basic Price',
    'BASIC_MARKET_ZERO': 'Price List (Basic) and Basic Price = 0',
    'BASIC_ZERO_MARKET_GT_ZERO': 'Price List (Basic) = 0 and Basic Price > 0',
}

def _party_flow_config_payload(config):
    rate_conditions = [
        condition
        for condition in (config.rate_conditions or [])
        if condition in RATE_CONDITION_CHOICES
    ]
    flow_type = _normalize_order_flow_type(config.flow_type)
    return {
        'card_code': config.card_code,
        'category': config.category or '',
        'flow_type': flow_type,
        'flow_label': ORDER_FLOW_TYPE_CHOICES.get(flow_type, flow_type),
        'rate_approval_enabled': bool(config.rate_approval_enabled),
        'billing_enabled': bool(config.billing_enabled),
        'auditor_enabled': bool(config.auditor_enabled),
        'rate_conditions': rate_conditions,
        'updated_at': config.updated_at.isoformat() if config.updated_at else None,
        'updated_by': getattr(config.updated_by, 'username', None),
    }

def _order_flow_config_payload(config=None, flow_type=ORDER_FLOW_TYPE_ASM):
    config = config or _get_order_flow_config(flow_type)
    rate_conditions = [
        condition
        for condition in (config.rate_conditions or [])
        if condition in RATE_CONDITION_CHOICES
    ]
    return {
        'flow_type': config.flow_type,
        'flow_label': ORDER_FLOW_TYPE_CHOICES.get(config.flow_type, config.flow_type),
        'flow_options': [
            {'code': code, 'label': label}
            for code, label in ORDER_FLOW_TYPE_CHOICES.items()
        ],
        'rate_approval_enabled': bool(config.rate_approval_enabled),
        'billing_enabled': bool(config.billing_enabled),
        'auditor_enabled': bool(config.auditor_enabled),
        'rate_conditions': rate_conditions,
        'condition_options': [
            {'code': code, 'label': label}
            for code, label in RATE_CONDITION_CHOICES.items()
        ],
        'updated_at': config.updated_at.isoformat() if config.updated_at else None,
        'updated_by': getattr(config.updated_by, 'username', None),
    }

# Page key (see frontend GRANTABLE_ADMIN_PAGES) that unlocks Order Flow Settings.
ORDER_FLOW_PAGE_KEY = 'Order_Flow_Settings'


def _can_manage_order_flow(user):
    """Admins, or any holder of the Order Flow Settings key.

    Phase 3: resolved through `core.permissions.effective_keys`, which reads
    the same `extra_pages` grant this function used to read directly — plus
    role bundles, so a role can now carry the page. `is_admin` replaces the
    local staff-or-primary-role-admin check: it also counts `is_superuser`
    and `admin` held via `extra_roles`, which the old comparison missed.
    """
    from core.permissions import effective_keys, is_admin

    return is_admin(user) or ORDER_FLOW_PAGE_KEY in effective_keys(user)


class OrderFlowConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        flow_type = _normalize_order_flow_type(request.query_params.get('flow_type'))
        return Response(_order_flow_config_payload(flow_type=flow_type))

    def post(self, request):
        if not _can_manage_order_flow(request.user):
            return Response({'message': 'Only admin can update order flow.'}, status=status.HTTP_403_FORBIDDEN)

        flow_type = _normalize_order_flow_type(request.data.get('flow_type'))
        config = _get_order_flow_config(flow_type)
        rate_conditions = request.data.get('rate_conditions', [])
        if not isinstance(rate_conditions, list):
            return Response({'message': 'rate_conditions must be a list.'}, status=status.HTTP_400_BAD_REQUEST)

        invalid_conditions = [
            condition for condition in rate_conditions if condition not in RATE_CONDITION_CHOICES
        ]
        if invalid_conditions:
            return Response(
                {'message': 'Invalid rate condition selected.', 'invalid_conditions': invalid_conditions},
                status=status.HTTP_400_BAD_REQUEST,
            )

        config.rate_approval_enabled = bool(request.data.get('rate_approval_enabled', False))
        config.billing_enabled = bool(request.data.get('billing_enabled', False))
        config.auditor_enabled = bool(request.data.get('auditor_enabled', False))
        config.flow_type = flow_type
        config.rate_conditions = list(dict.fromkeys(rate_conditions))
        config.updated_by = request.user
        config.save()

        return Response({
            'success': True,
            'message': 'Order flow updated successfully.',
            'data': _order_flow_config_payload(config),
        })

class PartyOrderFlowConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def _is_admin(self, request):
        # Admins, or users granted the Order Flow Settings page on Permissions.
        return _can_manage_order_flow(request.user)

    def _party_name_map(self, card_codes):
        """Resolve party names from the category-aware sap_parties table.

        The same card_code can belong to different parties across categories
        (e.g. CUSTA000878 is PURAN STORE under OIL and A ONE BEVERAGES under
        BEVERAGES), so we key by (card_code, category). A code-only fallback is
        kept for configs whose category is blank/unmatched.
        """
        if not card_codes:
            return {}, {}
        keyed = {}
        by_code = {}
        try:
            for row in SapParty.objects.filter(card_code__in=card_codes).values('card_code', 'card_name', 'category'):
                cat = (row.get('category') or '').strip().upper()
                keyed[(row['card_code'], cat)] = row['card_name']
                by_code.setdefault(row['card_code'], row['card_name'])
        except Exception:
            return {}, {}
        return keyed, by_code

    def get(self, request):
        configs = list(PartyOrderFlowConfig.objects.all().order_by('card_code', 'flow_type'))
        keyed_names, code_names = self._party_name_map(list({cfg.card_code for cfg in configs}))
        data = []
        for cfg in configs:
            payload = _party_flow_config_payload(cfg)
            cat = (cfg.category or '').strip().upper()
            payload['card_name'] = keyed_names.get((cfg.card_code, cat)) or code_names.get(cfg.card_code, '')
            data.append(payload)
        return Response({
            'success': True,
            'data': data,
            'flow_options': [
                {'code': code, 'label': label}
                for code, label in ORDER_FLOW_TYPE_CHOICES.items()
            ],
            'condition_options': [
                {'code': code, 'label': label}
                for code, label in RATE_CONDITION_CHOICES.items()
            ],
        })

    def _parse_parties(self, request):
        """Accept either parties=[{card_code, category}] or a plain card_codes list."""
        parties = request.data.get('parties')
        result = []
        if isinstance(parties, list) and parties:
            for entry in parties:
                if isinstance(entry, dict):
                    code = str(entry.get('card_code') or '').strip()
                    category = _normalize_category(entry.get('category'))
                else:
                    code = str(entry or '').strip()
                    category = ''
                if code:
                    result.append((code, category))
        else:
            for code in request.data.get('card_codes', []) or []:
                code = str(code).strip()
                if code:
                    result.append((code, ''))
        # de-dupe
        return list(dict.fromkeys(result))

    def post(self, request):
        if not self._is_admin(request):
            return Response({'message': 'Only admin can update order flow.'}, status=status.HTTP_403_FORBIDDEN)

        parties = self._parse_parties(request)
        if not parties:
            return Response({'message': 'Select at least one party.'}, status=status.HTTP_400_BAD_REQUEST)

        flow_type = _normalize_order_flow_type(request.data.get('flow_type'))

        rate_conditions = request.data.get('rate_conditions', [])
        if not isinstance(rate_conditions, list):
            return Response({'message': 'rate_conditions must be a list.'}, status=status.HTTP_400_BAD_REQUEST)
        invalid_conditions = [c for c in rate_conditions if c not in RATE_CONDITION_CHOICES]
        if invalid_conditions:
            return Response(
                {'message': 'Invalid rate condition selected.', 'invalid_conditions': invalid_conditions},
                status=status.HTTP_400_BAD_REQUEST,
            )

        values = {
            'rate_approval_enabled': bool(request.data.get('rate_approval_enabled', False)),
            'billing_enabled': bool(request.data.get('billing_enabled', False)),
            'auditor_enabled': bool(request.data.get('auditor_enabled', False)),
            'rate_conditions': list(dict.fromkeys(rate_conditions)),
            'updated_by': request.user,
        }

        saved = []
        for code, category in parties:
            cfg, _created = PartyOrderFlowConfig.objects.update_or_create(
                card_code=code, category=category, flow_type=flow_type, defaults=values
            )
            saved.append(_party_flow_config_payload(cfg))

        flow_label = ORDER_FLOW_TYPE_CHOICES.get(flow_type, flow_type)
        return Response({
            'success': True,
            'message': f'{flow_label} applied to {len(saved)} part{"y" if len(saved) == 1 else "ies"}.',
            'data': saved,
        })

    def delete(self, request):
        if not self._is_admin(request):
            return Response({'message': 'Only admin can update order flow.'}, status=status.HTTP_403_FORBIDDEN)

        parties = self._parse_parties(request)
        if not parties:
            return Response({'message': 'Select at least one party.'}, status=status.HTTP_400_BAD_REQUEST)

        flow_type = _normalize_order_flow_type(request.data.get('flow_type'))
        condition = Q()
        for code, category in parties:
            condition |= Q(card_code=code, category=category, flow_type=flow_type)
        PartyOrderFlowConfig.objects.filter(condition).delete()
        return Response({
            'success': True,
            'message': f'Removed custom flow for {len(parties)} part{"y" if len(parties) == 1 else "ies"}.',
            'removed': [{'card_code': code, 'category': category} for code, category in parties],
        })
