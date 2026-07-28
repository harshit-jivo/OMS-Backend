"""Fast SAP business-partner lookup for the invoice entry form.

Reads the business-partner master (OCRD) directly from HANA — NOT via the
Service Layer, which is slower — reusing the shared HANAConnection. Includes
BOTH vendors/suppliers (CardType 'S') AND customers (CardType 'C'), so the
entry form's searchable dropdown can pick either. Cached for a few minutes so
the dropdown stays snappy.

GSTIN is taken from OCRD."LicTradNo" (the Federal Tax ID field, which holds the
GSTIN in the India localization); if that is blank we fall back to any non-empty
CRD1."GSTRegnNo" for the party.
"""
from django.conf import settings
from django.core.cache import cache

from hana.services.connection import HANAConnection

# v2: cache key bumped so the supplier-only cache is discarded and customers appear.
VENDOR_CACHE_KEY = 'tracker_sap_vendors_v2'
VENDOR_CACHE_TTL = 600  # seconds (10 min)


def _vendor_query():
    schema = settings.DATABASES['hana']['OIL_SCHEMA']
    return f'''
        SELECT
            T0."CardCode"   AS "card_code",
            T0."CardName"   AS "card_name",
            T0."CardType"   AS "card_type",
            T0."LicTradNum" AS "lic_trad_no",
            T0."State1"     AS "state",
            G."gst_regn_no"
        FROM "{schema}"."OCRD" AS T0
        LEFT JOIN (
            SELECT "CardCode", MAX("GSTRegnNo") AS "gst_regn_no"
            FROM "{schema}"."CRD1"
            WHERE "GSTRegnNo" IS NOT NULL AND "GSTRegnNo" <> ''
            GROUP BY "CardCode"
        ) AS G ON G."CardCode" = T0."CardCode"
        WHERE T0."CardType" IN ('S', 'C')
        ORDER BY T0."CardName"
    '''


def fetch_vendors(force_refresh=False):
    """List of vendors: [{card_code, card_name, gstin, state}]. Cached."""
    if not force_refresh:
        cached = cache.get(VENDOR_CACHE_KEY)
        if cached is not None:
            return cached

    with HANAConnection() as conn:
        rows = conn.execute(_vendor_query())

    vendors = [{
        'card_code': (r.get('card_code') or '').strip(),
        'card_name': (r.get('card_name') or '').strip(),
        # 'S' = vendor/supplier, 'C' = customer.
        'card_type': (r.get('card_type') or '').strip(),
        'gstin': ((r.get('lic_trad_no') or '') or (r.get('gst_regn_no') or '')).strip(),
        'state': (r.get('state') or '').strip(),
    } for r in rows]

    cache.set(VENDOR_CACHE_KEY, vendors, VENDOR_CACHE_TTL)
    return vendors
