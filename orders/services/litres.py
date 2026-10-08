"""Which items an order line may count litres for.

`sal_pack_unit` (SAP OITM.SalPackUn) equals the litres in one piece only for
liquid stock: FG (packed oil / beverages) and RM (loose oil). On PM, CG and SC
items -- packaging, consumables, gift articles -- it is SAP's default of 1 or an
unrelated number, so qty x pack unit reported 25 empty tins as 25 litres.
Mirrors the frontend's `src/lib/itemUnits.ts`.
"""

LITRE_ITEM_PREFIXES = ('FG', 'RM')


def is_litre_item(item_code):
    return str(item_code or '').strip().upper().startswith(LITRE_ITEM_PREFIXES)
