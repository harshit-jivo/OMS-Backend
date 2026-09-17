import logging
import json
import re
import urllib3
from contextlib import contextmanager
from datetime import date, datetime
from functools import wraps
from django.db import connection as default_connection, transaction
from django.db.models import Q
from django.utils import timezone
from ..models import Product, Party, PartyAddress, Branch, SyncLog 
from .connection import SAPConnection
import requests
from django.conf import settings
from ..models import SalesQuotationLog , SalesOrderLog, SalesCancelledLog
from orders.services.scheme_rules import (
    get_party_product_scheme,
)
from users.models import PartyProductAssignment, SchemeProduct
from hana.services.services import SalesOrderService

logger = logging.getLogger(__name__)


class DuplicateCustomerReference(Exception):
    """The document's NumAtCard is already on another document for this BP.

    Raised before posting so the caller gets the reference and the blocking
    document number, instead of SAP's opaque "-5002 ... duplicated
    customer/vendor reference number".
    """


def get_scheme_item_code_raw(scheme_id):
    if not scheme_id:
        return None

    return (
        SchemeProduct.objects
        .filter(scheme_id=scheme_id)
        .values_list('item_code', flat=True)
        .first()
    )


def get_scheme_item_codes_for_combo(scheme_id, state_code=None):
    if not scheme_id:
        return []

    seed = (
        SchemeProduct.objects
        .filter(scheme_id=scheme_id)
        .values("scheme_name", "state_code")
        .first()
    )
    if not seed or not seed.get("scheme_name"):
        item_code = get_scheme_item_code_raw(scheme_id)
        return [item_code] if item_code else []

    # A combo scheme is several scheme_product rows sharing one scheme_name, so the
    # fan-out matches on that name. But scheme_name describes the OFFER ("1 free pcs
    # per pcs"), not the gift, and the same name is reused once per state with a
    # different item_code — e.g. '1 PCS CANOLA 1 LTR PER PCS' gives FG0000407
    # (water) in PB/HR and FG0000032 (cold-press oil) in DL. Matching on name alone
    # unions every state together and ships another state's gift for free.
    # scheme_id already pins name + state + item, so honour the state that was
    # actually picked; an explicit state_code argument overrides it.
    effective_state = state_code or seed.get("state_code")

    queryset = SchemeProduct.objects.filter(
        scheme_name=seed["scheme_name"],
        is_active=True,
    )
    if effective_state:
        queryset = queryset.filter(state_code=effective_state)

    item_codes = []
    seen = set()
    for item_code in queryset.order_by("scheme_id").values_list("item_code", flat=True):
        if not item_code or item_code in seen:
            continue
        seen.add(item_code)
        item_codes.append(item_code)

    return item_codes


def get_party_combo_item_codes(card_code, scheme_id, category=None, state_code=None):
    if not card_code or not scheme_id:
        return []

    queryset = PartyProductAssignment.objects.filter(
        card_code=card_code,
        scheme_id=scheme_id,
        is_active=True,
    )
    if category:
        queryset = queryset.filter(category=category)

    item_codes = []
    seen = set()
    for item_code in queryset.order_by("id").values_list("item_code", flat=True):
        if not item_code or item_code in seen:
            continue
        seen.add(item_code)
        item_codes.append(item_code)

    return item_codes




def _normalize_combo_component_name(value):
    text = str(value or "").upper()
    text = re.sub(r"\b\d+(?:\.\d+)?\s*PCS?\b", " ", text)
    text = re.sub(r"\bPCS?\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_party_combo_component_item_codes(card_code, item_name, category=None, exclude_item_code=None):
    if not card_code or not item_name or "+" not in str(item_name):
        return []

    components = [
        _normalize_combo_component_name(part)
        for part in str(item_name).split("+")
        if _normalize_combo_component_name(part)
    ]
    if not components:
        return []

    exclude_normalized = str(exclude_item_code).strip().upper() if exclude_item_code else None

    assignments = PartyProductAssignment.objects.filter(
        card_code=card_code,
        is_active=True,
    )
    if category:
        assignments = assignments.filter(category=category)

    item_codes = []
    seen = set()
    for assignment in assignments.order_by("item_code"):
        code = str(assignment.item_code or "").strip()
        if not code:
            continue
        # Explicitly skip the main combo item code
        if exclude_normalized and code.upper() == exclude_normalized:
            continue

        product = Product.objects.filter(
            item_code=assignment.item_code,
            category=assignment.category,
        ).first()
        product_name = _normalize_combo_component_name(
            getattr(product, "item_name", None)
        )
        logger.warning(
            "COMBO_COMPONENT_DEBUG | code=%r product=%r product_name=%r",
            code, getattr(product, "item_name", None), product_name,
        )
        if not product_name:
            continue

        # Skip products that are themselves combo items
        if "+" in getattr(product, "item_name", "") or "+" in product_name:
            continue

        matched = [c for c in components if c and c in product_name]
        unmatched = [c for c in components if c and c not in product_name]
        logger.warning(
            "COMBO_COMPONENT_DEBUG | code=%r matched=%r unmatched=%r",
            code, matched, unmatched,
        )
        if matched and unmatched:
            if code in seen:
                continue
            seen.add(code)
            item_codes.append(code)

    logger.warning("COMBO_COMPONENT_DEBUG | final item_codes=%r", item_codes)
    return item_codes


def _resolve_combo_parent_item_code(item):
    """The paid product a mapped combo bills as, or None.

    A combo pack ("A + B") is a wrapper around two real products. OMS keeps the
    combo itself on the order -- it is what the customer bought, what the order
    history shows, and what scheme triggers match on -- while SAP is sent the
    products the pack actually consists of. Swapping the code here, at the
    boundary, is the only place that needs to know the two representations
    differ.

    Quantity and price are deliberately untouched: the customer agreed the
    combo's rate, so the parent line carries it and the SAP document total still
    matches the order.

    Returns None when the combo has no parent mapped, in which case its own code
    goes to SAP exactly as before -- so an unmapped combo is unaffected.

    Gated on the "+" in the stored line name so an order without a combo never
    queries the mapping table: this runs per line on every SAP post, and
    map_order_to_sap is otherwise pure for orders that carry no combo.
    """
    if "+" not in str(getattr(item, "item_name", "") or ""):
        return None

    code = str(getattr(item, "item_code", "") or "").strip()
    if not code:
        return None
    category = getattr(item, "category", "")

    mapped = PartyProductAssignment.objects.filter(
        item_code=code, is_active=True,
    ).exclude(parent_item_code__isnull=True).exclude(parent_item_code="")

    # A combo maps to the same two products for every party, so any row will do.
    # Prefer the line's own category, then fall back across categories the same
    # way the order path's donor lookup does.
    parent = None
    if category:
        parent = mapped.filter(category=category).values_list(
            "parent_item_code", flat=True).first()
    if not parent:
        parent = mapped.values_list("parent_item_code", flat=True).first()

    parent = str(parent or "").strip()
    return parent or None


def print_sap_payload(label, payload):
    formatted_payload = json.dumps(payload, indent=2, default=str)
    print(f"{label}\n{formatted_payload}")
    logger.warning("%s\n%s", label, formatted_payload)


def _readable_sap_error(text):
    """Pull the human-readable reason out of a SAP Service Layer error body.

    The Service Layer returns errors as
    {"error": {"code": N, "message": {"lang": "...", "value": "the reason"}}}.
    We surface that `value` so the user sees *why* SAP refused (e.g. "Cannot
    cancel a document with a base/target document"), not the raw JSON. Falls
    back to the raw text for anything that isn't that shape (e.g. a transport
    error with no body).
    """
    if not text:
        return 'SAP did not return an error message.'
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return str(text).strip()
    err = data.get('error') if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get('message')
        if isinstance(msg, dict) and msg.get('value'):
            return str(msg['value']).strip()
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return str(text).strip()


def _to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _get_sap_line_quantity(item):
    if isinstance(item, dict):
        qty = _to_float(item.get("qty"), 0)
        if qty <= 0:
            qty = _to_float(item.get("boxes"), 0)
    else:
        qty = _to_float(getattr(item, "qty", None), 0)
        if qty <= 0:
            qty = _to_float(getattr(item, "boxes", None), 0)
    return qty


# An FOC line ships free, but SAP still needs a non-zero value: an invoice
# totalling 0 does not generate an IRN, so the billing team has always keyed a
# token rate by hand (0.001 on 70 lines, 0.01 on 105, 0.1 on 31). This is that
# convention made automatic. 0.001 keeps the invoice at the Rs 1 minimum for
# every realistic quantity.
FOC_TOKEN_UNIT_PRICE = 0.001


def _get_sap_unit_price(item, is_foc=False):
    price_list_basic = _to_float(getattr(item, "price_list_basic", None), 0)
    basic_price = _to_float(getattr(item, "basic_price", None), 0)

    if basic_price > 0:
        return basic_price

    if is_foc:
        # Never fall through to the price list on an FOC order. `basic_price` 0
        # with a live `price_list_basic` is exactly how a giveaway gets invoiced
        # at full value -- the line is free, so the token rate is the answer.
        return FOC_TOKEN_UNIT_PRICE

    # Not FOC: a blank basic price means the operator priced the line off the
    # price list, which is the intended rate. Returning 0 here would invoice a
    # real sale as free.
    return price_list_basic


def _iter_related_schemes(item):
    related_schemes = getattr(item, "schemes", None)
    if not related_schemes:
        return []

    if hasattr(related_schemes, "all"):
        related_schemes = related_schemes.all()

    return list(related_schemes or [])


def _get_order_item_scheme_entries(item, card_code, item_code, category):
    """Return [(raw_scheme_id, qty, snapshot_item_code), ...] for one order line.

    `snapshot_item_code` is `OrderItemScheme.benefit_item_code`, written at order
    creation. When it is present the caller ships exactly that item and never
    re-resolves from the scheme tables — otherwise editing a scheme would change
    what an already-approved order sends to SAP. It is None for rows predating
    the snapshot, which fall back to the legacy name-based fan-out.
    """
    entries = []

    for item_scheme in _iter_related_schemes(item):
        scheme_obj = getattr(item_scheme, "scheme", None)
        raw_scheme_id = (
            getattr(item_scheme, "scheme_id", None)
            or getattr(scheme_obj, "scheme_id", None)
        )
        scheme_qty = _to_float(getattr(item_scheme, "qty_scheme", None), 0)
        snapshot = (getattr(item_scheme, "benefit_item_code", None) or "").strip() or None
        if (raw_scheme_id not in (None, "") or snapshot) and scheme_qty > 0:
            entries.append((raw_scheme_id, scheme_qty, snapshot))

    if entries:
        return entries

    scheme_obj = getattr(item, "scheme", None)
    raw_scheme_id = item.__dict__.get("scheme_id") or getattr(scheme_obj, "scheme_id", None)
    scheme_qty = _to_float(getattr(item, "qty_scheme", None), 0)

    if raw_scheme_id in (None, "") and scheme_qty > 0:
        scheme_obj = get_party_product_scheme(
            card_code=card_code,
            item_code=item_code,
            category=category,
        )
        raw_scheme_id = getattr(scheme_obj, "scheme_id", None)

    if raw_scheme_id not in (None, "") and scheme_qty > 0:
        return [(raw_scheme_id, scheme_qty, None)]

    return []


def get_party_item_unit_price(card_code, item_code, category=None, default=0.0):
    if not card_code or not item_code:
        return float(default)

    queryset = PartyProductAssignment.objects.filter(
        card_code=card_code,
        item_code=item_code,
        is_active=True,
    )
    if category:
        queryset = queryset.filter(category=category)

    assignment = queryset.order_by("id").first()
    if assignment:
        return _to_float(getattr(assignment, "basic_rate", None), default)

    return float(default)


def serialized_sync(sync_type):
    """Refuse to start a sync while another run of the same type is in flight.

    Before the tables had unique constraints, two overlapping runs quietly
    inserted duplicate rows — that is how ~2952 duplicates accumulated in
    sap_party_addresses. Now the same race raises IntegrityError inside
    _bulk_upsert's atomic block and rolls the entire sync back, so the second
    run has to be turned away at the door rather than left to collide.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            with self._sync_lock(sync_type) as acquired:
                if not acquired:
                    return self._already_running(sync_type)
                return func(self, *args, **kwargs)
        return wrapper
    return decorator


class SyncService:
    # Namespace for this app's Postgres advisory locks, so the ids below can't
    # collide with a lock taken anywhere else in the project.
    _SYNC_LOCK_NAMESPACE = 19740
    _SYNC_LOCK_IDS = {
        'ALL': 1,
        'PRODUCT': 2,
        'PARTY': 3,
        'PARTY_ADDRESS': 4,
        'BRANCH': 5,
    }

    def __init__(self, triggered_by='manual'):
        self.triggered_by = triggered_by
        self.connection = SAPConnection()
        self.sap_timeout = (
            getattr(settings, "HANA_CONNECT_TIMEOUT", None) or 15,
            getattr(settings, "HANA_READ_TIMEOUT", None) or 120,
        )

    def _post_with_ssl_fallback(self, url, payload):
        """POST to the Service Layer, honouring the configured TLS setting.

        The name is kept because callers use it; the FALLBACK is deliberately
        gone. It used to catch SSLError, retry with `verify=False`, and leave
        verification off for the rest of this object's life.

        That is worse than never verifying at all. It yields at exactly the
        moment verification is doing its job — a certificate that does not
        validate is the case it exists to catch — and then sends the SAP
        credential and a sales order over the connection it just failed to
        authenticate. Worse, it did so silently, so a deployment could believe
        it had TLS verification on for months.

        A certificate failure is now a failure. The two supported ways to talk
        to a SAP box with a self-signed certificate are both explicit:
        `HANA_SSL_CA_BUNDLE` to trust that certificate, or
        `HANA_SSL_VERIFY=false` to accept the risk on purpose.
        """
        return self.sap_session.post(
            url,
            json=payload,
            verify=self.sap_verify,
            timeout=self.sap_timeout,
        )

    @contextmanager
    def _sync_lock(self, sync_type):
        """Hold a Postgres session advisory lock for the duration of a sync.

        Session-scoped rather than transaction-scoped, because a sync spends
        most of its time querying SAP outside any transaction. Yields True when
        the lock was taken, False when another run already holds it. If the
        process dies the lock dies with its connection, so nothing gets wedged.

        Note: this relies on the connection being a real session. Behind a
        transaction-pooling proxy such as pgbouncer, session advisory locks do
        not behave as expected.
        """
        lock_id = self._SYNC_LOCK_IDS[sync_type]
        args = [self._SYNC_LOCK_NAMESPACE, lock_id]
        with default_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", args)
            acquired = bool(cursor.fetchone()[0])
        try:
            yield acquired
        finally:
            if acquired:
                with default_connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s, %s)", args)

    @staticmethod
    def _already_running(sync_type):
        message = (
            f"A {sync_type} sync is already running; this run was skipped."
        )
        logger.warning(message)
        return {
            'success': False,
            'skipped': True,
            'processed': 0,
            'created': 0,
            'updated': 0,
            'error': message,
        }

    @staticmethod
    def _as_trimmed(value, max_len=None):
        text = '' if value is None else str(value).strip()
        if max_len and len(text) > max_len:
            return text[:max_len]
        return text

    @staticmethod
    def _chunked(items, size):
        items = list(items)
        for start in range(0, len(items), size):
            yield items[start:start + size]

    def _bulk_upsert(self, model, key_fields, incoming, touch_field=None, batch_size=1000):
        """Upsert `incoming` in bulk instead of one update_or_create per row.

        `incoming` maps a key tuple (aligned with `key_fields`) to the dict of
        non-key fields to write. Existing rows are loaded with a handful of
        `key_fields[0] __in` queries rather than one SELECT per row, and the
        writes go out as batched INSERT/UPDATE inside a single transaction.

        Returns (created_count, updated_count).
        """
        if not incoming:
            return 0, 0

        now = timezone.now()
        lead_field = key_fields[0]
        lead_values = {key[0] for key in incoming}

        existing = {}
        for chunk in self._chunked(sorted(lead_values, key=lambda v: (v is None, v)), 500):
            for obj in model.objects.filter(**{f'{lead_field}__in': chunk}):
                existing[tuple(getattr(obj, f) for f in key_fields)] = obj

        update_fields = sorted({field for data in incoming.values() for field in data})
        if touch_field:
            update_fields.append(touch_field)

        to_create = []
        to_update = []
        for key, data in incoming.items():
            obj = existing.get(key)
            if obj is None:
                init = dict(zip(key_fields, key))
                init.update(data)
                if touch_field:
                    init[touch_field] = now
                to_create.append(model(**init))
            else:
                for field, value in data.items():
                    setattr(obj, field, value)
                if touch_field:
                    setattr(obj, touch_field, now)
                to_update.append(obj)

        with transaction.atomic():
            model.objects.bulk_create(to_create, batch_size=batch_size)
            model.objects.bulk_update(to_update, update_fields, batch_size=batch_size)

        return len(to_create), len(to_update)

    @staticmethod
    def _normalize_order_category(value):
        return str(value or "").strip().upper()

    def _resolve_item_sub_group(self, item):
        """The line's profit-center (sub_group). Prefer the value stored on the
        order item; when blank, fall back to the synced product master
        (sap_products) by item_code + category. Because MART products are stored
        under category='MART', a Mart line resolves its sub_group against MART
        products — exactly the way OIL/BEVERAGE lines do.
        """
        stored = str(getattr(item, "sub_group", "") or "").strip()
        if stored:
            return stored

        item_code = str(getattr(item, "item_code", "") or "").strip()
        if not item_code:
            return ""

        product_query = Product.objects.filter(item_code__iexact=item_code)
        category = str(getattr(item, "category", "") or "").strip()
        if category:
            product_query = product_query.filter(category__iexact=category)
        product = product_query.first()
        return str(getattr(product, "sub_group", "") or "").strip() if product else ""

    @staticmethod
    def _resolve_address_code(address_id):
        """SAP's ShipToCode / PayToCode is the CRD1 Address *name* (a short code,
        e.g. 'SHIP1'), not the full address text. The order stores the
        sap_party_addresses PK in ship_to_id / bill_to_id, so resolve the name
        from there. Returns None when there's no id/match (SAP then uses the BP's
        default address).
        """
        try:
            address_id = int(address_id or 0)
        except (TypeError, ValueError):
            return None
        if not address_id:
            return None
        return (
            PartyAddress.objects.filter(id=address_id)
            .values_list("address_name", flat=True)
            .first()
        ) or None

    # Order.company == '3' means the Mart company (distributor orders). See
    # DISTRIBUTOR_COMPANY in orders/views.py.
    MART_COMPANY_CODE = "3"

    def resolve_company_db_for_order(self, order):
        default_company_db = settings.HANA_OIL_COMPANY_DB

        # Company 3 (Mart / distributor) is a separate SAP company. Route it to
        # the Mart CompanyDB based purely on the order's company code, before any
        # item-category logic — a company-3 order always books into Mart.
        order_company = str(getattr(order, "company", "") or "").strip()
        if order_company == self.MART_COMPANY_CODE:
            mart_company_db = getattr(settings, "HANA_MART_COMPANY_DB", "")
            if mart_company_db:
                return mart_company_db
            logger.warning(
                "Order %s is company 3 (Mart) but HANA_MART_COMPANY_DB is not "
                "configured; falling back to default CompanyDB=%s",
                getattr(order, "id", None),
                default_company_db,
            )
            return default_company_db

        beverages_company_db = (
            getattr(settings, "HANA_COMPANY_DB_BEVERAGES", "") or default_company_db
        )

        categories = []
        for item in order.items.all():
            category = self._normalize_order_category(getattr(item, "category", ""))
            if category and category not in categories:
                categories.append(category)

        if not categories:
            return default_company_db

        if len(categories) > 1:
            logger.warning(
                "Mixed order categories found for order %s: %s. Falling back to default CompanyDB=%s",
                getattr(order, "id", None),
                categories,
                default_company_db,
            )
            return default_company_db

        if categories[0] == "BEVERAGES":
            return beverages_company_db

        return default_company_db

    def resolve_warehouse_code_for_order(self, order, category):
        """The warehouse every line of this order ships from.

        The order carries one chosen warehouse for all its lines. Only when it
        has none — orders placed before the picker existed — does the
        per-category default in settings apply.
        """
        chosen = str(getattr(order, "warehouse_code", "") or "").strip()
        if chosen:
            return chosen
        return self.resolve_warehouse_code_for_category(category)

    def resolve_warehouse_code_for_category(self, category):
        normalized_category = self._normalize_order_category(category)
        default_warehouse_code = str(
            getattr(settings, "HANA_WAREHOUSE_CODE", "GP-FG") or ""
        ).strip()
        beverages_warehouse_code = str(
            getattr(settings, "HANA_WAREHOUSE_CODE_BEVERAGES", "") or ""
        ).strip()

        if normalized_category == "BEVERAGES":
            return beverages_warehouse_code

        return default_warehouse_code
   
    @serialized_sync('ALL')
    def sync_all(self):
        log = SyncLog.objects.create(
            sync_type='ALL',
            status='STARTED',
            triggered_by=self.triggered_by
        )
       
        total_processed = 0
        total_created = 0
        total_updated = 0
        errors = []
       
        try:
            # Sync Products
            result = self.sync_products()
            total_processed += result['processed']
            total_created += result['created']
            total_updated += result['updated']
            if result.get('error'):
                errors.append(f"Products: {result['error']}")
           
            # Sync Parties
            result = self.sync_parties()
            total_processed += result['processed']
            total_created += result['created']
            total_updated += result['updated']
            if result.get('error'):
                errors.append(f"Parties: {result['error']}")
           
            # Sync Party Addresses
            result = self.sync_party_addresses()
            total_processed += result['processed']
            total_created += result['created']
            total_updated += result['updated']
            if result.get('error'):
                errors.append(f"Party Addresses: {result['error']}")
           
            # Sync Branches
            result = self.sync_branches()
            total_processed += result['processed']
            total_created += result['created']
            total_updated += result['updated']
            if result.get('error'):
                errors.append(f"Branches: {result['error']}")
           
            log.status = 'SUCCESS' if not errors else 'FAILED'
            log.records_processed = total_processed
            log.records_created = total_created
            log.records_updated = total_updated
            log.error_message = '\n'.join(errors) if errors else None
            log.completed_at = timezone.now()
            log.save()
           
            return {
                'success': not errors,
                'processed': total_processed,
                'created': total_created,
                'updated': total_updated,
                'errors': errors
            }
           
        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()
            raise
   
    @serialized_sync('PRODUCT')
    def sync_products(self):
        log = SyncLog.objects.create(
            sync_type='PRODUCT',
            status='STARTED',
            triggered_by=self.triggered_by
        )
       
        created_count = 0
        updated_count = 0
        processed_count = 0
       
        try:
            with self.connection as conn:
                query = SAPConnection.get_products_query()
                results = conn.execute_query(query)
               
                # Collapse to one payload per (item_code, category) so duplicate
                # source rows don't turn into conflicting writes.
                incoming = {}
                for row in results:
                    processed_count += 1
                    item_code = row.get('ItemCode')
                    category = row.get('Category')  # OIL, BEVERAGES, or MART

                    if not item_code:
                        continue

                    # Data to update (excluding lookup fields)
                    incoming[(item_code, category)] = {
                        'item_name': row.get('ItemName'),
                        'sal_factor2': row.get('SalFactor2'),
                        'tax_rate': row.get('U_Rev_tax_Rate'),
                        #'tax_code': row.get('TaxCode'),
                        'is_deleted': row.get('Deleted', 'N'),
                        'variety': row.get('U_Variety'),
                        'type': row.get('U_TYPE'),
                        'sub_group': row.get('U_Sub_Group'),
                        'sal_pack_unit': row.get('SalPackUn'),
                        'brand': row.get('U_Brand'),
                        'on_hand': row.get('OnHand'),
                        'is_active': row.get('validFor'),
                    }

                # Lookup by BOTH item_code AND category
                created_count, updated_count = self._bulk_upsert(
                    Product,
                    ['item_code', 'category'],
                    incoming,
                    touch_field='synced_at',
                )

            log.status = 'SUCCESS'
            log.records_processed = processed_count
            log.records_created = created_count
            log.records_updated = updated_count
            log.completed_at = timezone.now()
            log.save()
           
            return {
                'success': True,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count
            }
           
        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()
           
            return {
                'success': False,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count,
                'error': str(e)
            }
   
    @serialized_sync('PARTY')
    def sync_parties(self):
        log = SyncLog.objects.create(
            sync_type='PARTY',
            status='STARTED',
            triggered_by=self.triggered_by
        )

        created_count = 0
        updated_count = 0
        processed_count = 0

        try:
            with self.connection as conn:
                query = SAPConnection.get_parties_query()
                results = conn.execute_query(query)

                # Collapse to one payload per (card_code, category) so duplicate
                # source rows don't turn into conflicting writes.
                incoming = {}
                for row in results:
                    processed_count += 1
                    card_code = row.get('CardCode')
                    category = row.get('Category') or ''  # ✅ OIL/BEVERAGES/MART

                    if not card_code:
                        continue

                    incoming[(card_code, category)] = {
                        'card_name': row.get('CardName') or '',
                        'address': row.get('Address') or '',
                        'state': row.get('State1') or '',
                        'main_group': row.get('U_Main_Group') or '',
                        'chain': row.get('U_Chain') or '',
                        'country': row.get('Country') or '',
                        'card_type': row.get('CardType') or 'C',
                    }

                created_count, updated_count = self._bulk_upsert(
                    Party,
                    ['card_code', 'category'],  # ✅ Lookup by both
                    incoming,
                    touch_field='synced_at',
                )

            log.status = 'SUCCESS'
            log.records_processed = processed_count
            log.records_created = created_count
            log.records_updated = updated_count
            log.completed_at = timezone.now()
            log.save()

            return {
                'success': True,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count
            }

        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()

            return {
                'success': False,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count,
                'error': str(e)
            }

    @serialized_sync('PARTY_ADDRESS')
    def sync_party_addresses(self):
        log = SyncLog.objects.create(
            sync_type='PARTY_ADDRESS',
            status='STARTED',
            triggered_by=self.triggered_by
        )
           
        created_count = 0
        updated_count = 0
        processed_count = 0
        row_errors = []

        try:
            with self.connection as conn:
                query = SAPConnection.get_party_addresses_query()
                results = conn.execute_query(query)
                
                # print("\n================================================================================\n")
                # print(json.dumps(results, indent=4))
                # print("\n================================================================================\n")
                
                # Collapse the SAP rows into one payload per unique key first, so
                # duplicates in the source don't turn into conflicting writes.
                incoming = {}
                for row in results:
                    processed_count += 1
                    card_code = self._as_trimmed(row.get('CardCode'), 50)
                    address_name = self._as_trimmed(row.get('Address'), 100)
                    category = self._as_trimmed(row.get('Category'), 20)
                    # Part of the key: CRD1 holds a B and an S row per Address,
                    # each carrying its own GSTRegnNo.
                    address_type = self._as_trimmed(row.get('AdresType') or 'B', 1) or 'B'

                    if not card_code:
                        continue

                    try:
                        incoming[(card_code, address_name, category, address_type)] = {
                            'gst_number': self._as_trimmed(row.get('GSTRegnNo'), 50),
                            'state': self._as_trimmed(row.get('State'), 100),
                            'city': self._as_trimmed(row.get('City'), 100),
                            'zip_code': self._as_trimmed(row.get('ZipCode'), 20),
                            'country': self._as_trimmed(row.get('Country'), 50),
                            'full_address': self._as_trimmed(row.get('MainAddress')),
                        }
                    except Exception as row_error:
                        row_errors.append(
                            f"{card_code}/{address_name or '-'}: {str(row_error)}"
                        )

                created_count, updated_count = self._bulk_upsert(
                    PartyAddress,
                    ['card_code', 'address_name', 'category', 'address_type'],
                    incoming,
                    touch_field='synced_at',
                )

            log.status = 'SUCCESS'
            log.records_processed = processed_count
            log.records_created = created_count
            log.records_updated = updated_count
            if row_errors:
                log.error_message = '\n'.join(row_errors[:25])
            log.completed_at = timezone.now()
            log.save()
            
            print(f"""
                            'success': {True},
                            'processed': {processed_count},
                            'created': {created_count},
                            'updated': {updated_count},
                            'warnings': {row_errors[:25]}
                        
            """)

            return {
                'success': True,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count,
                'warnings': row_errors[:25]
            }

        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()
            
            print(f"""
                                        'success': {False},
                                        'processed': {processed_count},
                                        'created': {created_count},
                                        'updated': {updated_count},
                                        'error': {str(e)}
                                    
                        """)
            return {
                'success': False,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count,
                'error': str(e)
            }

    @serialized_sync('BRANCH')
    def sync_branches(self):
        """Sync branches from SAP OBPL table"""
        log = SyncLog.objects.create(
            sync_type='BRANCH',
            status='STARTED',
            triggered_by=self.triggered_by
        )
       
        created_count = 0
        updated_count = 0
        processed_count = 0
       
        try:
            with self.connection as conn:
                query = SAPConnection.get_branches_query()
                results = conn.execute_query(query)
               
                incoming = {}
                for row in results:
                    processed_count += 1
                    bpl_id = row.get('BPLId')
                    category = row.get('Category')  # OIL, BEVERAGES, or MART

                    if bpl_id is None:
                        continue

                    incoming[(bpl_id, category)] = {
                        'bpl_name': row.get('BPLName'),
                    }

                # Lookup by BOTH bpl_id AND category (same item can exist in multiple DBs)
                created_count, updated_count = self._bulk_upsert(
                    Branch,
                    ['bpl_id', 'category'],
                    incoming,
                    touch_field='updated_at',
                )

            log.status = 'SUCCESS'
            log.records_processed = processed_count
            log.records_created = created_count
            log.records_updated = updated_count
            log.completed_at = timezone.now()
            log.save()
           
            return {
                'success': True,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count
            }
           
        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()
           
            return {
                'success': False,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count,
                'error': str(e)
            }

        # ---------------- SAP LOGIN ---------------- #

    def sap_login(self, company_db=None, username=None, password=None):
        login_url = f"{settings.HANA_SERVICE_LAYER_URL}/Login"
        print("sap login url:", login_url)
        company_db = company_db or settings.HANA_OIL_COMPANY_DB
        username = username or settings.HANA_USERNAME
        password = password or settings.HANA_PASSWORD

        payload = {
            "CompanyDB": company_db,
            "UserName": username,
            "Password": password
        }

        print_sap_payload("SAP Login Payload:", payload)

        self.sap_session = requests.Session()
        self.sap_verify = (
            settings.HANA_SSL_CA_BUNDLE
            if getattr(settings, "HANA_SSL_CA_BUNDLE", "")
            else getattr(settings, "HANA_SSL_VERIFY", True)
        )
        if self.sap_verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.sap_session.verify = self.sap_verify
       
        try:
            response = self._post_with_ssl_fallback(login_url, payload)
        except requests.RequestException as e:
            raise Exception(
                f"SAP Login connection failed ({login_url}, timeout={self.sap_timeout}): {str(e)}"
            )

        if response.status_code != 200:
            raise Exception(f"SAP Login Failed: {response.text}")

        login_data = response.json()
        self.sap_company_db = company_db
        self.sap_username = username
        logger.info(
            "SAP Login success | CompanyDB=%s | User=%s | SessionId=%s",
            company_db,
            username,
            login_data.get("SessionId", "N/A"),
        )
        return login_data
   
    # ---------------- MAP ORDER ---------------- #

    def map_order_to_sap(self, order):
        def _as_iso_date(value, fallback):
            if isinstance(value, datetime):
                return value.date().isoformat()
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value).date().isoformat()
                except ValueError:
                    return fallback.isoformat()
            return fallback.isoformat()

        # The order can belong to the OIL, BEVERAGE or MART company; resolve which
        # so the OPRC (costing code) lookup hits the right schema. Company '3' is
        # the Mart company (distributor orders).
        company_db = self.resolve_company_db_for_order(order)
        beverages_company_db = getattr(settings, "HANA_COMPANY_DB_BEVERAGES", "") or None
        order_company = str(getattr(order, "company", "") or "").strip()
        if order_company == self.MART_COMPANY_CODE:
            branch = "MART"
        elif beverages_company_db and company_db == beverages_company_db:
            branch = "BEVERAGE"
        else:
            branch = "OIL"

        sales_service = SalesOrderService()
        costing_code_cache = {}

        def _resolve_costing_code(prc_name):
            """Map a profit-center name (sub_group) to its SAP CostingCode (PrcCode).

            Returns None when the name cannot be resolved, and the caller then
            OMITS CostingCode from the line. Falling back to the raw name (as
            this used to) is worse than sending nothing: a name over 8 chars is
            rejected outright by SAP ("Value too long in property 'CostingCode'",
            e.g. SUNFLOWER), and one *under* 8 chars is accepted and books the
            line to a profit center that may not be the intended one. Never
            raises -- an unresolvable name must not fail the whole mapping.
            """
            if not prc_name:
                return None
            if prc_name not in costing_code_cache:
                try:
                    resolved = sales_service.get_costing_code(prc_name, branch)
                except Exception as exc:
                    logger.warning(
                        "Failed to resolve costing code for profit center %r (%s): %s",
                        prc_name, branch, exc,
                    )
                    resolved = None
                if resolved is None:
                    logger.warning(
                        "No active PrcCode found in OPRC dimension 1 for profit "
                        "center %r (%s); sending the line with NO CostingCode.",
                        prc_name, branch,
                    )
                costing_code_cache[prc_name] = resolved
            return costing_code_cache[prc_name]

        # Mart lines don't resolve a profit center per sub_group (its OPRC has
        # only the default General Center); use a single configured code, or none.
        mart_costing_code = str(getattr(settings, "HANA_MART_COSTING_CODE", "") or "").strip()

        document_lines = []

        for item in order.items.all():
            print("\n============================================\n")
            print(getattr(item, "sub_group", None))
            print("\n ============================================\n")

            order_qty = _get_sap_line_quantity(item)  
            item_unit_price = _get_sap_unit_price(
                item, is_foc=bool(getattr(order, "is_foc", False)))
            card_code = getattr(order, "card_code", "")
            item_code = getattr(item, "item_code", "")
            category = getattr(item, "category", "")
            sub_group = getattr(item, "sub_group", "")

            # Prefer the item's stored sub_group; fall back to the product master
            # by item_code + category (MART products for a Mart line).
            sub_group = self._resolve_item_sub_group(item)

            # CostingCode = the profit-center dimension SAP labels the "Variety"
            # column in the Mart company (a mandatory line field there). For every
            # company it resolves from the line's sub_group via OPRC — the Mart
            # profit centers are named by sub_group (e.g. CANOLA/MUSTARD -> PrcCode
            # CANOLA/MUSTARD). Mart may pin an explicit code via
            # HANA_MART_COSTING_CODE when set.
            if branch == "MART" and mart_costing_code:
                line_costing_code = mart_costing_code
            else:
                line_costing_code = _resolve_costing_code(sub_group)

            warehouse_code = self.resolve_warehouse_code_for_order(order, category)
            scheme_entries = _get_order_item_scheme_entries(
                item,
                card_code=card_code,
                item_code=item_code,
                category=category,
            )

            is_scheme_line = getattr(item, 'item_type', '') == 'SCHEME'

            # A mapped combo reaches SAP as the product it actually is; the
            # combo code stays on the OMS order and never leaves it.
            sap_item_code = _resolve_combo_parent_item_code(item) or item_code
            if sap_item_code != item_code:
                logger.info(
                    "Combo %s posted to SAP as its parent %s (order %s)",
                    item_code, sap_item_code, getattr(order, "id", None),
                )

            # Line 1: always the ordered item at its price
            if not is_scheme_line and (order_qty > 0 or item_unit_price > 0):
                line = {
                    "ItemCode": sap_item_code,
                    "Quantity": order_qty,
                    "UnitPrice": item_unit_price,
                    "U_SchemeAgst": sub_group,
                }
                if line_costing_code:
                    line["CostingCode"] = line_costing_code
                if warehouse_code:
                    line["WarehouseCode"] = warehouse_code
                document_lines.append(line)

            for raw_scheme_id, scheme_qty, snapshot_item_code in scheme_entries:
                if snapshot_item_code:
                    # Snapshot taken at order creation — authoritative. Editing
                    # the scheme afterwards must not change what this order ships.
                    scheme_item_codes = [snapshot_item_code]
                else:
                    scheme_item_codes = []
                    try:
                        raw_scheme_id_int = int(raw_scheme_id)
                        scheme_item_codes = get_scheme_item_codes_for_combo(
                            raw_scheme_id_int,
                        )
                    except (SchemeProduct.DoesNotExist, TypeError, ValueError):
                        logger.warning(
                            "Skipping invalid scheme mapping for order item %s with raw scheme_id=%r",
                            getattr(item, "id", None),
                            raw_scheme_id,
                        )

                if not scheme_item_codes:
                    logger.warning(
                        "Scheme %r on order item %s resolved to NO giveaway item; "
                        "no free line will be sent.",
                        raw_scheme_id, getattr(item, "id", None),
                    )
                    
                if not scheme_item_codes:
                    logger.warning(
                        "Scheme %r on order item %s resolved to NO giveaway item; "
                        "no free line will be sent.",
                        raw_scheme_id, getattr(item, "id", None),
                    )

                for scheme_item_code in scheme_item_codes:
                    # A scheme whose giveaway IS the ordered item ("buy 3 boxes, get
                    # 2 pcs of the same free") is legitimate and must still be sent —
                    # as its own zero-price line, so the paid line keeps its price.
                    # Skipping it here silently dropped the customer's free stock.
                    line = {
                        "ItemCode": scheme_item_code,
                        "Quantity": scheme_qty,
                        "UnitPrice": 0.0,
                        "U_SchemeAgst": sub_group,
                    }
                    if line_costing_code:
                        line["CostingCode"] = line_costing_code
                    if warehouse_code:
                        line["WarehouseCode"] = warehouse_code
                    document_lines.append(line)

       
        posting_date = timezone.localdate()
        due_date = _as_iso_date(getattr(order, "delivery_date", None), posting_date)

        payload = {
            "CardCode": order.card_code,
            "DocDate": posting_date.isoformat(),
            "DocDueDate": due_date,
            "TaxDate": posting_date.isoformat(),
            "Comments": " ",
            # SAP wants the CRD1 Address *name* (a short code, max 50 chars), not
            # the full address text. The order-entry page happens to store the
            # name in ship_to_address/bill_to_address, so sending those worked --
            # but the Distributor page stores the real street address there, and
            # SAP rejects it with "Value too long in property 'PayToCode'".
            # Resolving from the id is identical for party orders (the stored
            # text already IS the name) and correct for Mart. Falls back to the
            # stored text when the id resolves to nothing, so behaviour is
            # unchanged wherever it cannot be resolved.
            "ShipToCode": self._resolve_address_code(order.ship_to_id) or order.ship_to_address,
            "PayToCode": self._resolve_address_code(order.bill_to_id) or order.bill_to_address,
            #=-===============================================================================
            # CHANGE AND MAP THE SALES PERSON
            #=-===============================================================================
            
            #=================================================================================
            "BPL_IDAssignedToInvoice": order.dispatch_from_id,
            "DocumentLines": document_lines,
        }
        # Uppercased because SBO_SP_TransactionNotification rejects a document
        # whose NumAtCard differs from its own uppercase ("Vendor Reff should be
        # in Upper Case", error 1713256). Most references are numeric, where this
        # is a no-op, but ~39% carry letters.
        po_number = str(getattr(order, "po_number", "") or "").strip().upper()
        if po_number:
            payload["NumAtCard"] = po_number

        print_sap_payload(
            f"SAP quotation payload for order {getattr(order, 'id', 'N/A')}:",
            payload,
        )
   
        return payload

# example
# def map_order_to_sap(self, order):
#         return {
#                 "CardCode": "CUSTA000486",
#                 "DocDate": "2026-02-10",
#                 "DocDueDate": "2026-02-15",
#                 "TaxDate": "2026-02-10",
#                 "NumAtCard": "7801514523",
#                 "Comments": " ",
#                 "ShipToCode": "WAL MART INDIA PVT LTD LUDHIANA 4717",
#                 "PayToCode": "WAL MART INDIA PVT LTD LUDHIANA 4717",
#                 "BPL_IDAssignedToInvoice" : 2,
#                 "DocumentLines": [
#                   {
#                     "ItemCode": "FG0000145",
#                     "Quantity": 84,
#                     "UnitPrice": 1286,
#                     "WarehouseCode": "GP-FG"
#                   }
#                 ]
#         }

    # ---------------- CREATE SALES QUOTATION ---------------- #

    def _branch_for_company_db(self, company_db):
        """'OIL' or 'BEVERAGE' for a resolved company DB (the HANA query key)."""
        beverages_company_db = getattr(settings, "HANA_COMPANY_DB_BEVERAGES", "") or None
        return "BEVERAGE" if (beverages_company_db and company_db == beverages_company_db) else "OIL"

    def _assert_num_at_card_available(self, payload, company_db, table):
        """Fail before posting if this customer reference is already taken.

        SAP rejects a duplicate with a bare "-5002 ... duplicated customer/vendor
        reference number" that names neither the reference nor the document
        holding it, which makes the real cause (usually a retry of a submission
        that actually succeeded) hard to see. Checking first lets us say exactly
        which document is in the way.

        Scoped to one document type and one business partner -- see
        Queries.get_duplicate_num_at_card for why a wider check would be wrong.
        Never blocks on a lookup failure: HANA being unreachable must not stop a
        posting that SAP would have accepted.
        """
        num_at_card = str(payload.get("NumAtCard") or "").strip()
        card_code = payload.get("CardCode")
        if not num_at_card or not card_code:
            return

        try:
            existing = SalesOrderService().find_duplicate_num_at_card(
                num_at_card, card_code, self._branch_for_company_db(company_db), table,
            )
        except Exception as exc:
            logger.warning(
                "Could not pre-check NumAtCard %r for %s in %s: %s",
                num_at_card, card_code, table, exc,
            )
            return

        if existing:
            doc_nums = ", ".join(str(row.get("DocNum")) for row in existing)
            raise DuplicateCustomerReference(
                f"Customer reference {num_at_card!r} is already used by {card_code} "
                f"on {table} document(s) {doc_nums}. SAP will reject a duplicate. "
                f"If this is a retry, that document is the one already created."
            )

    def create_sales_quotation(self, order):

        quotation_payload = self.map_order_to_sap(order)
        company_db = self.resolve_company_db_for_order(order)

        log = SalesQuotationLog.objects.create(
            order_id=order.id, 
            status='STARTED',
            request_data=quotation_payload
        )

        try:
            # Before the login round trip: a duplicate reference is a certain
            # rejection, and failing here names the document in the way.
            self._assert_num_at_card_available(quotation_payload, company_db, 'OQUT')

            if (
                not hasattr(self, 'sap_session')
                or getattr(self, 'sap_company_db', None) != company_db
                or getattr(self, 'sap_username', None) != settings.HANA_USERNAME
            ):
                self.sap_login(company_db=company_db)

            url = f"{settings.HANA_SERVICE_LAYER_URL}/Quotations"
            print(f"SAP quotation URL: {url}")
            logger.warning("SAP quotation URL: %s", url)

            response = self._post_with_ssl_fallback(url, quotation_payload)
            logger.info(
                "SAP Quotations response | status=%s | body=%s",
                response.status_code,
                response.text[:500],
            )
            if response.status_code == 201:
                response_data = response.json()

                # order.status = 6
                order.save(update_fields=['status'])  
               
                order.sap_created = True
                order.save(update_fields=['sap_created'])

                log.status = 'SUCCESS'
                log.response_data = response_data
                log.sap_doc_entry = response_data.get("DocEntry")
                log.sap_doc_num = response_data.get("DocNum")
                log.completed_at = timezone.now()
                log.save()

                return response_data

            else:
                log.status = 'FAILED'
                log.response_data = response.text
                log.error_message = response.text
                log.completed_at = timezone.now()
                log.save()

                raise Exception(response.text)

        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()
            raise


    def cancel_sales_order(self, order, doc_entry, reason=None):
        """Cancel an already-created SAP Sales Order via the Service Layer.

        `POST /Orders(DocEntry)/Cancel` is SAP B1's canonical way to cancel a
        sales order — it sets the document's status to Cancelled (returning 204
        No Content on success). This is a real financial reversal, so it is only
        ever called for an order that actually reached SAP; `doc_entry` is the
        SAP DocEntry recorded on the order's successful SalesOrderLog.

        SAP refuses the cancel when the order has already been copied to a
        Delivery or Invoice ("...base document..."), which is correct — those
        downstream documents must be cancelled first. We surface SAP's message
        rather than force it, and the OMS status is only moved by the caller
        AFTER this returns cleanly, so a refusal leaves OMS unchanged.

        Logged to SalesCancelledLog (its own `sales_cancellation_logs` table,
        the cancel-side counterpart of SalesOrderLog), keyed by order_id and
        carrying the DocEntry and the cancellation reason, so the audit trail
        shows every cancel attempt start-to-response.
        """
        # One Service Layer credential set; the Mart/Oil difference is the
        # company DB, resolved inside the try below so a misconfiguration is
        # recorded on the cancel log rather than thrown before it exists.
        order_user = settings.SALES_ORDER_USER
        order_password = settings.SALES_ORDER_PASSWORD

        log = SalesCancelledLog.objects.create(
            order_id=order.id,
            status='STARTED',
            sap_doc_entry=doc_entry,
            cancellation_reason=(reason or '').strip() or None,
            request_data={'action': 'cancel', 'DocEntry': doc_entry},
        )

        try:
            # A distributor order is ALWAYS company 3 (Mart), so the cancel must
            # log into the Mart company DB — never the OIL fallback the category
            # resolver would use. Fail loudly if Mart isn't configured.
            company_db = str(getattr(settings, 'HANA_MART_COMPANY_DB', '') or '').strip()
            if not company_db:
                raise Exception(
                    "HANA_MART_COMPANY_DB is not configured; cannot cancel a Mart "
                    "sales order without the Mart company database."
                )
            log.request_data = {
                'action': 'cancel', 'DocEntry': doc_entry, 'company_db': company_db,
            }
            log.save(update_fields=['request_data'])

            if (
                not hasattr(self, 'sap_session')
                or getattr(self, 'sap_company_db', None) != company_db
                or getattr(self, 'sap_username', None) != order_user
            ):
                self.sap_login(
                    company_db=company_db,
                    username=order_user,
                    password=order_password,
                )

            url = f"{settings.HANA_SERVICE_LAYER_URL}/Orders({int(doc_entry)})/Cancel"
            print(f"SAP order cancel URL: {url}")
            logger.warning("SAP order cancel URL: %s", url)

            response = self.sap_session.post(
                url,
                verify=self.sap_verify,
                timeout=self.sap_timeout,
            )
            logger.info(
                "SAP Cancel response | status=%s | body=%s",
                response.status_code,
                (response.text or '')[:500],
            )

            # 204 No Content is the documented success; accept 200 defensively.
            if response.status_code in (200, 204):
                log.status = 'SUCCESS'
                log.response_data = response.text or 'Cancelled'
                log.completed_at = timezone.now()
                log.save()
                return {'cancelled': True, 'doc_entry': doc_entry}

            log.status = 'FAILED'
            log.response_data = response.text          # raw body, for audit
            log.error_message = _readable_sap_error(response.text)  # the reason
            log.completed_at = timezone.now()
            log.save()
            raise Exception(log.error_message)

        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()
            raise


    def create_sales_order(self, order):

        order_payload = self.map_order_to_sap(order)
        company_db = self.resolve_company_db_for_order(order)

        log = SalesOrderLog.objects.create(
            order_id=order.id,
            status='STARTED',
            request_data=order_payload
        )

        order_user = settings.SALES_ORDER_USER
        order_password = settings.SALES_ORDER_PASSWORD

        try:
            self._assert_num_at_card_available(order_payload, company_db, 'ORDR')

            if (
                not hasattr(self, 'sap_session')
                or getattr(self, 'sap_company_db', None) != company_db
                or getattr(self, 'sap_username', None) != order_user
            ):
                self.sap_login(
                    company_db=company_db,
                    username=order_user,
                    password=order_password,
                )

            url = f"{settings.HANA_SERVICE_LAYER_URL}/Orders"
            print(f"SAP order URL: {url}")
            logger.warning("SAP order URL: %s", url)

            print(order_payload)
            response = self._post_with_ssl_fallback(url, order_payload)
            logger.info(
                "SAP Orders response | status=%s | body=%s",
                response.status_code,
                response.text[:500],
            )
            if response.status_code == 201:
                response_data = response.json()

                # The order's status transition (→ Completed) is owned by the
                # caller (MartApproveView); here we only record the SAP result.
                order.sap_created = True
                order.save(update_fields=['sap_created'])

                log.status = 'SUCCESS'
                log.response_data = response_data
                log.sap_doc_entry = response_data.get("DocEntry")
                log.sap_doc_num = response_data.get("DocNum")
                log.completed_at = timezone.now()
                log.save()

                return response_data

            else:
                log.status = 'FAILED'
                log.response_data = response.text
                log.error_message = response.text
                log.completed_at = timezone.now()
                log.save()

                raise Exception(response.text)

        except Exception as e:
            log.status = 'FAILED'
            log.error_message = str(e)
            log.completed_at = timezone.now()
            log.save()
            raise
