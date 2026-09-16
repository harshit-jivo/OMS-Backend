"""What SAP's release gate would actually do with an order.

OMS cannot change SAP's rule — it is a literal inside
`SBO_SP_TRANSACTIONNOTIFICATION`, a 22,000-line procedure. What OMS can do is
stop the rule's blind spots from being invisible.

The OIL gate reads, in full:

    A."Status" = 'R' AND A."Type" = 'S'
    AND (P."Status" <> 'A' OR P."Status" IS NULL)
    AND O."Series" <> 392          -- raw materials are exempt
    AND A."UserSign" <> 33         -- ONE USER is exempt

`UserSign 33` is USER24, Gautam Chanana, who created **2,888 of the 3,152**
eligible orders since JSAP went live. As written the gate applies to about 8%
of production. An approval that was never going to be enforced should not look
identical to one that was, so `gate_exempt` is on the API and visible on every
request.

These values MIRROR SAP; they do not drive it. Changing them here changes
nothing in SAP — it only changes what OMS reports. Keep them in step with the
procedure, or the reporting quietly becomes fiction.
"""
from django.conf import settings

from production.models import SapOrderType

#: `OITM.Series` values SAP's gate exempts. 392 is RM (raw materials).
EXEMPT_ITEM_SERIES = frozenset(
    getattr(settings, 'PRODUCTION_GATE_EXEMPT_SERIES', (392,)))

#: `OWOR.UserSign` values SAP's gate exempts. Settings-driven so that when the
#: exemption is finally removed from the procedure it is one config change
#: here, not a code edit.
EXEMPT_USER_SIGNS = frozenset(
    getattr(settings, 'PRODUCTION_GATE_EXEMPT_USER_SIGNS', (33,)))


def is_exempt(order):
    """Would SAP have released this order without any approval at all?

    True does NOT mean the approval was pointless — it means SAP would not
    have stopped the order regardless, so the approval is a record rather than
    a control. That difference is worth showing an auditor.
    """
    if order.order_type != SapOrderType.STANDARD:
        return True
    if order.item_series in EXEMPT_ITEM_SERIES:
        return True
    return order.sap_user_sign in EXEMPT_USER_SIGNS


def exemption_reason(order):
    """Why SAP's gate would skip this order, or None."""
    if order.order_type != SapOrderType.STANDARD:
        return (f'SAP only gates Standard orders; this one is '
                f'{order.get_order_type_display()}.')
    if order.item_series in EXEMPT_ITEM_SERIES:
        return f'Item series {order.item_series} is exempt from SAP\'s gate.'
    if order.sap_user_sign in EXEMPT_USER_SIGNS:
        return (f'SAP exempts orders raised by user id {order.sap_user_sign} '
                f'({order.sap_created_by or "unknown"}) from the release gate.')
    return None
