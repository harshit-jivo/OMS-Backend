"""Helpers shared across the order views.

Extracted from the 5,919-line `orders/views.py` as the first step of splitting
it (plan item 3.1). Nothing here is order-flow logic — these are the scoping
predicates that several unrelated view groups needed, and being needed by
everything is part of what kept the file indivisible.

These belong in `orders/selectors.py` eventually. They are here first because a
move within `orders.views` is invisible to every caller, and a move to a new
top-level module is not.
"""
from urllib import request
from django.shortcuts import render
import re
from sap_sync.models import Branch
from orders.serializers import SchemeProductSerializer,OrderDetailSerializer, OrderListByUserIdSerializer,OrdersLogSerializer,OrderStatusUpdateSerializer, DispatchLocationSerializer,BranchSerializer, PartyAddressSerializer,ProductSerializer,CreateOrderSerializer,OrderItemSerializer, CreateSchemeSerializer,SchemeWriteSerializer,OrderItemSchemeSerializer, NotificationSerializer,StaffProductSerializer , OrdersByItemSerializer
from orders.models import PartyProductAssignment,OrdersLog,Parties, DispatchLocation, UserPartyAssignment, PartyAddress,ProductDetails,Order,OrderItem,OrderStatus,log_order_action, OrderItemScheme,OrderItemScheme,Template, Notification, PushToken, WebPushSubscription, StaffProductPrice, OrderFlowConfig, PartyOrderFlowConfig, RateApproverRule,OrderRateApproval,OrderItemApprovalMapping
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from rest_framework.permissions import IsAdminUser
import calendar
from django.db.models import Sum, Count, Max, F, Q, OuterRef, Subquery
from django.db.models.functions import TruncMonth
from django.utils import timezone
from collections import defaultdict
from django.shortcuts import get_object_or_404
from rest_framework import permissions
from sap_sync.models import Party as SapParty, PartyAddress as SapPartyAddress, Product as SapProduct, active_product_q, SalesQuotationLog, SalesOrderLog
from sap_sync.services.connection import SAPConnection
from orders.models import Order, OrderStatus
from orders.models import PartyProductAssignment
from orders.scheme_rules import (
    get_ordered_quantity,
    get_party_product_scheme,
    should_mirror_punjab_combo_scheme_qty)
from orders import scheme_engine
from users.models import SchemeProduct, User, State
from django.http import Http404, JsonResponse
from django.db import transaction
import json
import logging
import requests
from orders.ai_service import get_order_summary
from orders.notifications import (
    NotificationEvents,
    NotificationPlan,
    NotificationTemplates,
    NotificationTypes,
    deactivate_push_token,
    deliver_notification,
    deliver_notification_to_many,
    mark_order_notifications_read,
)
from orders.webpush import get_vapid_public_key
from orders.webpush import get_vapid_public_key
from django.db import transaction as _db_transaction
from orders.models import Scheme, SchemeAssignment
from orders.serializers import SchemeV2Serializer, SchemeAssignmentSerializer

logger = logging.getLogger(__name__)



def _get_user_category_name(user):
    category_obj = getattr(user, 'category', None)
    category_name = getattr(category_obj, 'category', category_obj)
    normalized_category = str(category_name or '').strip().upper()
    return normalized_category or None


def _get_user_category_names(user):
    category = _get_user_category_name(user)
    return [category] if category else []


def _normalize_scope_name(value):
    normalized = str(value or '').strip()
    return normalized or None


def _get_user_main_group_names(user):
    names = []
    main_group = getattr(user, 'main_group', None)
    if main_group:
        names.append(_normalize_scope_name(getattr(main_group, 'name', main_group)))

    main_groups = getattr(user, 'main_groups', None)
    if main_groups is not None:
        for group in main_groups.all():
            names.append(_normalize_scope_name(getattr(group, 'name', group)))

    return list(dict.fromkeys(name for name in names if name))


def _build_iexact_filter(field_name, values):
    query = Q()
    for value in values:
        query |= Q(**{f'{field_name}__iexact': value})
    return query

def _assigned_rate_approvers_for_order(order, status_filter=None, exclude_user=None):
    approvals = order.rate_approvals.select_related('approver').all()
    if status_filter:
        approvals = approvals.filter(status=status_filter)
    if exclude_user:
        approvals = approvals.exclude(approver=exclude_user)
    return [approval.approver for approval in approvals if approval.approver]
