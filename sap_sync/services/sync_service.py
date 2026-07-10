import logging
import json
import re
import urllib3
from datetime import date, datetime
from django.db.models import Q
from django.utils import timezone
from ..models import Product, Party, PartyAddress, Branch, SyncLog 
from .connection import SAPConnection
import requests
from django.conf import settings
from ..models import SalesQuotationLog , SalesOrderLog
from orders.scheme_rules import (
    get_party_product_scheme,
)
from users.models import PartyProductAssignment, SchemeProduct

logger = logging.getLogger(__name__)


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

    queryset = SchemeProduct.objects.filter(
        scheme_name=seed["scheme_name"],
        is_active=True,
    )

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


def print_sap_payload(label, payload):
    formatted_payload = json.dumps(payload, indent=2, default=str)
    print(f"{label}\n{formatted_payload}")
    logger.warning("%s\n%s", label, formatted_payload)


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


def _get_sap_unit_price(item):
    price_list_basic = _to_float(getattr(item, "price_list_basic", None), 0)
    basic_price = _to_float(getattr(item, "basic_price", None), 0)

    if basic_price > 0:
        return basic_price

    return price_list_basic


def _iter_related_schemes(item):
    related_schemes = getattr(item, "schemes", None)
    if not related_schemes:
        return []

    if hasattr(related_schemes, "all"):
        related_schemes = related_schemes.all()

    return list(related_schemes or [])


def _get_order_item_scheme_entries(item, card_code, item_code, category):
    entries = []

    for item_scheme in _iter_related_schemes(item):
        scheme_obj = getattr(item_scheme, "scheme", None)
        raw_scheme_id = (
            getattr(item_scheme, "scheme_id", None)
            or getattr(scheme_obj, "scheme_id", None)
        )
        scheme_qty = _to_float(getattr(item_scheme, "qty_scheme", None), 0)
        if raw_scheme_id not in (None, "") and scheme_qty > 0:
            entries.append((raw_scheme_id, scheme_qty))

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
        return [(raw_scheme_id, scheme_qty)]

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


class SyncService:
    def __init__(self, triggered_by='manual'):
        self.triggered_by = triggered_by
        self.connection = SAPConnection()
        self.sap_timeout = (
            getattr(settings, "HANA_CONNECT_TIMEOUT", None) or 15,
            getattr(settings, "HANA_READ_TIMEOUT", None) or 120,
        )

    def _post_with_ssl_fallback(self, url, payload):
        try:
            return self.sap_session.post(
                url,
                json=payload,
                verify=self.sap_verify,
                timeout=self.sap_timeout,
            )
        except requests.exceptions.SSLError:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.sap_verify = False
            self.sap_session.verify = False
            return self.sap_session.post(
                url,
                json=payload,
                verify=False,
                timeout=self.sap_timeout,
            )

    @staticmethod
    def _as_trimmed(value, max_len=None):
        text = '' if value is None else str(value).strip()
        if max_len and len(text) > max_len:
            return text[:max_len]
        return text

    @staticmethod
    def _normalize_order_category(value):
        return str(value or "").strip().upper()

    def resolve_company_db_for_order(self, order):
        default_company_db = settings.HANA_COMPANY_DB
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
               
                for row in results:
                    processed_count += 1
                    item_code = row.get('ItemCode')
                    category = row.get('Category')  # OIL, BEVERAGES, or MART
                   
                    if not item_code:
                        continue
                   
                    # Data to update (excluding lookup fields)
                    product_data = {
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
                    product, created = Product.objects.update_or_create(
                        item_code=item_code,
                        category=category,
                        defaults=product_data
                    )
                   
                    if created:
                        created_count += 1
                    else:
                        updated_count += 1
           
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

                for row in results:
                    processed_count += 1
                    card_code = row.get('CardCode')
                    category = row.get('Category')  # ✅ OIL/BEVERAGES/MART

                    if not card_code:
                        continue

                    party_data = {
                        'card_name': row.get('CardName') or '',
                        'address': row.get('Address') or '',
                        'state': row.get('State1') or '',
                        'main_group': row.get('U_Main_Group') or '',
                        'chain': row.get('U_Chain') or '',
                        'country': row.get('Country') or '',
                        'card_type': row.get('CardType') or 'C',                        
                        'category': category or '',                    
                    }

                    party, created = Party.objects.update_or_create(
                        card_code=card_code,
                        category=category,  # ✅ Lookup by both
                        defaults=party_data
                    )

                    if created:
                        created_count += 1
                    else:
                        updated_count += 1

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

                for row in results:
                    processed_count += 1
                    card_code = self._as_trimmed(row.get('CardCode'), 50)
                    address_name = self._as_trimmed(row.get('Address'), 100)
                    category = self._as_trimmed(row.get('Category'), 20)

                    if not card_code:
                        continue

                    try:
                        address_data = {
                            'address_type': self._as_trimmed(row.get('AdresType') or 'B', 1) or 'B',
                            'gst_number': self._as_trimmed(row.get('GSTRegnNo'), 50),
                            'state': self._as_trimmed(row.get('State'), 100),
                            'city': self._as_trimmed(row.get('City'), 100),
                            'zip_code': self._as_trimmed(row.get('ZipCode'), 20),
                            'country': self._as_trimmed(row.get('Country'), 50),
                            'full_address': self._as_trimmed(row.get('MainAddress')),
                            'category': category,
                        }

                        _, created = PartyAddress.objects.update_or_create(
                            card_code=card_code,
                            address_name=address_name,
                            category=category,
                            defaults=address_data
                        )

                        if created:
                            created_count += 1
                        else:
                            updated_count += 1
                    except Exception as row_error:
                        row_errors.append(
                            f"{card_code}/{address_name or '-'}: {str(row_error)}"
                        )

            log.status = 'SUCCESS'
            log.records_processed = processed_count
            log.records_created = created_count
            log.records_updated = updated_count
            if row_errors:
                log.error_message = '\n'.join(row_errors[:25])
            log.completed_at = timezone.now()
            log.save()

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

            return {
                'success': False,
                'processed': processed_count,
                'created': created_count,
                'updated': updated_count,
                'error': str(e)
            }

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
               
                for row in results:
                    processed_count += 1
                    bpl_id = row.get('BPLId')
                    category = row.get('Category')  # OIL, BEVERAGES, or MART
                   
                    if bpl_id is None:
                        continue
                   
                    branch_data = {
                        'bpl_name': row.get('BPLName'),
                    }
                   
                    # Lookup by BOTH bpl_id AND category (same item can exist in multiple DBs)
                    branch, created = Branch.objects.update_or_create(
                        bpl_id=bpl_id,
                        category=category,
                        defaults=branch_data
                    )
                   
                    if created:
                        created_count += 1
                    else:
                        updated_count += 1
           
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

    def sap_login(self, company_db=None):
        login_url = f"{settings.HANA_SERVICE_LAYER_URL}/Login"
        print("sap login url:", login_url)
        company_db = company_db or settings.HANA_COMPANY_DB

        payload = {
            "CompanyDB": company_db,
            "UserName": settings.HANA_USERNAME,
            "Password": settings.HANA_PASSWORD
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
        logger.info(
            "SAP Login success | CompanyDB=%s | User=%s | SessionId=%s",
            company_db,
            settings.HANA_USERNAME,
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

        document_lines = []
           
        for item in order.items.all():
            print("\n============================================\n")
            print(getattr(item, "sub_group"))
            print("\n ============================================\n")

            order_qty = _get_sap_line_quantity(item)  
            item_unit_price = _get_sap_unit_price(item)
            card_code = getattr(order, "card_code", "")
            item_code = getattr(item, "item_code", "")
            category = getattr(item, "category", "")
            sub_group = getattr(item, "sub_group", "")
            warehouse_code = self.resolve_warehouse_code_for_category(category)
            scheme_entries = _get_order_item_scheme_entries(
                item,
                card_code=card_code,
                item_code=item_code,
                category=category,
            )

            is_scheme_line = getattr(item, 'item_type', '') == 'SCHEME'

            # Line 1: always the ordered item at its price
            if not is_scheme_line and (order_qty > 0 or item_unit_price > 0):
                line = {
                    "ItemCode": item_code,
                    "Quantity": order_qty,
                    "UnitPrice": item_unit_price,
                    "U_SchemeAgst": sub_group,
                    "CostingCode" : sub_group
                }
                if warehouse_code:
                    line["WarehouseCode"] = warehouse_code
                document_lines.append(line)

            for raw_scheme_id, scheme_qty in scheme_entries:
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

                for scheme_item_code in scheme_item_codes:
                    if scheme_item_code == item_code:
                        continue

                    line = {
                        "ItemCode": scheme_item_code,
                        "Quantity": scheme_qty,
                        "UnitPrice": 0.0,
                    
                    }
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
            "ShipToCode": order.ship_to_address,
            "PayToCode": order.bill_to_address,
            #=-===============================================================================
            # CHANGE AND MAP THE SALES PERSON
            #=-===============================================================================


            "U_SALES_PERSON" : "PUNJAB GT" ,
            
            #=================================================================================
            "BPL_IDAssignedToInvoice": order.dispatch_from_id,
            "DocumentLines": document_lines,
        }
        po_number = str(getattr(order, "po_number", "") or "").strip()
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

    def create_sales_quotation(self, order):
       
        quotation_payload = self.map_order_to_sap(order)
        company_db = self.resolve_company_db_for_order(order)

        log = SalesQuotationLog.objects.create(
            order_id=order.id,
            status='STARTED',
            request_data=quotation_payload
        )

        try:
            if (
                not hasattr(self, 'sap_session')
                or getattr(self, 'sap_company_db', None) != company_db
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


    def create_sales_order(self, order):
       
        quotation_payload = self.map_order_to_sap(order)
        company_db = self.resolve_company_db_for_order(order)

        log = SalesOrderLog.objects.create(
            order_id=order.id,
            status='STARTED',
            request_data=quotation_payload
        )

        try:
            if (
                not hasattr(self, 'sap_session')
                or getattr(self, 'sap_company_db', None) != company_db
            ):
                self.sap_login(company_db=company_db)
     
            url = f"{settings.HANA_SERVICE_LAYER_URL}/Orders"
            print(f"SAP order URL: {url}")
            logger.warning("SAP order URL: %s", url)

            response = self._post_with_ssl_fallback(url, quotation_payload)
            logger.info(
                "SAP Orders response | status=%s | body=%s",
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
