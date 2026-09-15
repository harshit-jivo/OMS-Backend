from .services.services import SalesOrderService

# Company databases the invoice queries can be pointed at.
VALID_BRANCHES = ('OIL', 'BEVERAGE', 'MART')


def normalize_branch(value, default='OIL'):
    """Map whatever the caller sent ('oil', 'bev', 'BEVERAGES', 'mart', '') to a
    branch.

    Returns None for anything that is not a recognised company, so callers can
    answer 400 rather than silently printing from the wrong database.
    """
    branch = str(value or '').strip().upper()
    if not branch:
        return default
    if branch.startswith('BEV'):
        return 'BEVERAGE'
    if branch.startswith('OIL'):
        return 'OIL'
    if branch.startswith('MART') or branch.startswith('JIVO_MART'):
        return 'MART'
    return None


def resolve_doc_entry(doc_num, branch='OIL'):
    """DocNum -> OINV."DocEntry" for the given company, or None if not found.

    DocNum is the number printed on the invoice; every downstream consumer
    (Crystal bill print, SAP Service Layer) keys off DocEntry instead, and the
    same DocNum can exist in every company database — hence the branch.
    """
    rows = SalesOrderService().get_docEntry(doc_num, branch)
    if not rows:
        return None
    return rows[0]['DocEntry']


def resolve_order_doc_entry(doc_num, branch='OIL'):
    """DocNum -> ORDR."DocEntry" for the given company, or None if not found.

    The sales-order counterpart of `resolve_doc_entry`. The Crystal sales-order
    layouts are addressed by DocEntry exactly as the bill print is, and the same
    DocNum exists in every company database — hence the branch here too.
    """
    rows = SalesOrderService().get_order_doc_entry(doc_num, branch)
    if not rows:
        return None
    return rows[0]['DocEntry']


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_pending_dispatch(rows, invoice_rows=None, dispatch_from=None):
    """Shape open sales orders and their invoices into the report.

    `rows` are every line of every open order (from
    `Queries.get_pending_dispatch`), `invoice_rows` the AR invoices raised
    against those orders (from `Queries.get_order_invoices`), and
    `dispatch_from` maps a SAP sales-order DocNum -> the OMS dispatch location.

    Everything here is read out of SAP -- nothing is typed in or kept in step by
    hand -- so the answer moves the moment an invoice is punched or a line is
    added to an order.

    The packaging maths is SAP's own, verified against the team's sheet:
      * OITM."SalFactor2" is the case pack (12 / 4 / 20 pcs per box)
      * OITM."SalPackUn" is the litres one piece holds
      * pieces / case pack = boxes, pieces x litres = total litres

    Returns orders, each carrying its lines and its invoice flow, so a caller
    can render SAP's relationship map without a second round trip. Because
    fully-billed lines are included, ordered - invoiced = pending holds at both
    line and order level.
    """
    dispatch_from = dispatch_from or {}

    invoices_by_order = {}
    for inv in invoice_rows or []:
        invoices_by_order.setdefault(inv['so_doc_entry'], []).append({
            "invoice_num": inv.get('invoice_num'),
            "invoice_entry": inv.get('invoice_entry'),
            "invoice_date": str(inv['invoice_date'])[:10] if inv.get('invoice_date') else None,
            # 'O' means the invoice is still unpaid/open in SAP; 'C' closed.
            "invoice_status": inv.get('invoice_status') or "",
            "invoice_total": _as_float(inv.get('invoice_total')),
            # Quantity and value drawn FROM THIS ORDER, which is not the whole
            # invoice when it also bills lines of another order.
            "qty": _as_float(inv.get('qty')),
            "amount": _as_float(inv.get('amount')),
            "line_count": int(inv.get('line_count') or 0),
        })

    report = []
    for row in rows:
        qty_ordered = _as_float(row.get('qty_ordered'))
        qty_pending = _as_float(row.get('qty_pending'))
        qty_invoiced = _as_float(row.get('qty_invoiced'))
        case_pack = _as_float(row.get('case_pack'))
        ltr_per_pc = _as_float(row.get('ltr_per_pc'))
        sales_order = row.get('sales_order')

        report.append({
            "order_date": str(row['order_date'])[:10] if row.get('order_date') else None,
            "delivery_date": str(row['delivery_date'])[:10] if row.get('delivery_date') else None,
            # An order raised straight in SAP has no OMS dispatch location, so
            # the line's own warehouse stands in rather than an empty cell.
            "dispatch_from": dispatch_from.get(str(sales_order))
            or row.get('warehouse_code') or "",
            "so_name": row.get('so_name') or "",
            "card_code": row.get('card_code') or "",
            "party_name": row.get('party_name') or "",
            "location": row.get('location') or "",
            "chain": row.get('chain') or "",
            "item_code": row.get('item_code') or "",
            "item_name": row.get('item_name') or "",
            "sku": row.get('sku') or "",
            "qty_ordered": qty_ordered,
            "qty_invoiced": qty_invoiced,
            "qty_pcs": qty_pending,
            "total_ltr": round(qty_pending * ltr_per_pc, 2),
            # A case pack of 0 would be a broken item master; guard rather than
            # raise, so one bad item cannot take the whole report down.
            "qty_boxes": round(qty_pending / case_pack, 2) if case_pack else 0,
            "sales_order": sales_order,
            "so_doc_entry": row['so_doc_entry'],
            "line_num": row['line_num'],
            # Two different questions, so two fields: what was billed on THIS
            # line, and every invoice raised against the order it sits on.
            "invoice": row.get('invoice_numbers') or "",
            "order_invoices": row.get('order_invoice_numbers') or "",
            "case_pack": case_pack,
            "per_ltr": ltr_per_pc,
            "box_ltr": round(case_pack * ltr_per_pc, 2),
            "brand": row.get('brand') or "",
            "oil_category": row.get('oil_category') or "",
            "category": row.get('category') or "",
            "case_pack_type": row.get('case_pack_type') or "",
            "variety": row.get('variety') or "",
            "warehouse_code": row.get('warehouse_code') or "",
            "line_total": _as_float(row.get('line_total')),
            "line_status": row.get('line_status') or "",
            # Where the line stands, computed once here rather than re-derived
            # by every client. A line can be fully billed while its order is
            # still open on other lines.
            "status": (
                "INVOICED" if qty_pending <= 0
                else "PARTLY INVOICED" if qty_invoiced
                else "PENDING"
            ),
        })

    return _group_into_orders(report, invoices_by_order)


def _group_into_orders(lines, invoices_by_order):
    """Fold report lines into their sales orders, with the invoice flow."""
    orders = {}

    for line in lines:
        entry = line['so_doc_entry']
        order = orders.get(entry)
        if order is None:
            order = orders[entry] = {
                "so_doc_entry": entry,
                "sales_order": line['sales_order'],
                "order_date": line['order_date'],
                "delivery_date": line['delivery_date'],
                "card_code": line['card_code'],
                "party_name": line['party_name'],
                "so_name": line['so_name'],
                "location": line['location'],
                "chain": line['chain'],
                "dispatch_from": line['dispatch_from'],
                "lines": [],
                "invoices": invoices_by_order.get(entry, []),
            }
        order['lines'].append(line)

    result = []
    for order in orders.values():
        lines_ = order['lines']
        pending = [line for line in lines_ if line['qty_pcs'] > 0]

        order['qty_ordered'] = round(sum(l['qty_ordered'] for l in lines_), 2)
        order['qty_invoiced'] = round(sum(l['qty_invoiced'] for l in lines_), 2)
        order['qty_pending'] = round(sum(l['qty_pcs'] for l in lines_), 2)
        order['ltr_pending'] = round(sum(l['total_ltr'] for l in pending), 2)
        order['boxes_pending'] = round(sum(l['qty_boxes'] for l in pending), 2)
        order['value_pending'] = round(sum(l['line_total'] for l in pending), 2)
        order['line_count'] = len(lines_)
        order['pending_line_count'] = len(pending)
        order['invoice_count'] = len(order['invoices'])
        order['invoiced_value'] = round(
            sum(inv['amount'] for inv in order['invoices']), 2
        )
        # Percent of the ordered quantity already billed -- the one number that
        # says how far along an order is at a glance.
        order['invoiced_pct'] = (
            round(order['qty_invoiced'] / order['qty_ordered'] * 100, 1)
            if order['qty_ordered'] else 0
        )
        order['status'] = (
            "PARTLY INVOICED" if order['invoice_count'] else "NOT INVOICED"
        )
        result.append(order)

    # Oldest order first: the one waiting longest is the one to chase.
    result.sort(key=lambda o: (o['order_date'] or "", o['sales_order'] or 0))
    return result


def build_inventory_report(rows):
    """Pivot flat item/warehouse stock rows into the Inventory Report shape.

    Rows in, one per item *and* warehouse; out, one entry per item carrying a
    `stock` map keyed by warehouse code, grouped by variety (OITM.U_Sub_Group)
    with a subtotal per group and a grand total across the whole report.

    The warehouse columns are whatever warehouses actually appear in `rows`,
    ordered by how much stock they hold, so the busy ones (BH-BT, BH-PF) sit
    first and an empty warehouse never earns a column at all. Every row-level
    total is the sum across *these* columns only -- so when the caller narrows
    the report to a few warehouses, the totals narrow with it instead of
    quietly reporting stock the user cannot see.
    """
    warehouse_names = {}
    warehouse_totals = {}
    items = {}

    for row in rows:
        code = row['warehouse_code']
        qty = float(row['on_hand'] or 0)
        item_code = row['item_code']

        warehouse_names.setdefault(code, row['warehouse_name'] or code)
        warehouse_totals[code] = warehouse_totals.get(code, 0.0) + qty

        item = items.get(item_code)
        if item is None:
            item = items[item_code] = {
                "item_code": item_code,
                "item_name": row['item_name'],
                "sku": row['sku'] or "",
                # Ungrouped items would otherwise collapse into a nameless
                # group; give them an explicit bucket that sorts last.
                "sub_group": (row['sub_group'] or "UNCATEGORISED").strip(),
                "variety": row['variety'] or "",
                "brand": row['brand'] or "",
                "stock": {},
                "total": 0.0,
            }
        # One item can hold stock in the same warehouse only once, but sum
        # anyway rather than overwrite -- a duplicate row must not go missing.
        item['stock'][code] = item['stock'].get(code, 0.0) + qty
        item['total'] += qty

    warehouses = [
        {"code": code, "name": warehouse_names[code]}
        for code in sorted(
            warehouse_totals,
            key=lambda code: (-warehouse_totals[code], code),
        )
    ]

    groups = {}
    for item in items.values():
        groups.setdefault(item['sub_group'], []).append(item)

    grouped = []
    for name in sorted(groups, key=lambda n: (n == "UNCATEGORISED", n)):
        group_items = sorted(groups[name], key=lambda i: -i['total'])
        totals = {}
        for item in group_items:
            for code, qty in item['stock'].items():
                totals[code] = totals.get(code, 0.0) + qty
        grouped.append({
            "sub_group": name,
            "items": group_items,
            "totals": totals,
            "total": sum(item['total'] for item in group_items),
        })

    return {
        "warehouses": warehouses,
        "groups": grouped,
        "totals": warehouse_totals,
        "grand_total": sum(warehouse_totals.values()),
        "item_count": len(items),
    }


def group_sales_orders(rows):
    orders = {}

    for row in rows:
        doc_entry = row['DocEntry']

        if doc_entry not in orders:
            orders[doc_entry] = {
                "DocEntry":   row['DocEntry'],
                "DocNum":     row['DocNum'],
                "DocDate":    str(row['DocDate']),
                "DocDueDate": str(row['DocDueDate']),
                "CardCode":   row['CardCode'],
                "CardName":   row['CardName'],
                "NumAtCard":  row['NumAtCard'],
                "DocStatus":  row['DocStatus'],
                "DocTotal":   float(row['DocTotal'] or 0),
                "VatSum":     float(row['VatSum'] or 0),
                "DiscSum":    float(row['DiscSum'] or 0),
                "Comments":   row['Comments'],
                "SlpCode":    row['SlpCode'],
                "ShipToCode" : row['ShipToCode'],
                "PayToCode" : row['PayToCode'],
                "BPL_Id" : row['BPLId'],
                "lines": []
            }

        orders[doc_entry]['lines'].append({
            "LineNum":    row['LineNum'],
            "ItemCode":   row['ItemCode'],
            "Dscription": row['Dscription'],
            "Quantity":   float(row['Quantity'] or 0),
            "OpenQty":    float(row['OpenQty'] or 0),
            "Price":      float(row['Price'] or 0),
            "PriceBefDi": float(row['PriceBefDi'] or 0),
            "DiscPrcnt":  float(row['DiscPrcnt'] or 0),
            "LineTotal":  float(row['LineTotal'] or 0),
            "VatPrcnt":   float(row['VatPrcnt'] or 0),
            "VatGroup":   row['VatGroup'],
            "WhsCode":    row['WhsCode'],
            "TaxCode":    row['TaxCode'],
            "ShipDate":   str(row['ShipDate']),
            "AcctCode":   row['AcctCode'],
            "Project":    row['Project'],
            "OcrCode":    row['OcrCode'],
            "LineStatus": row['LineStatus'],
        })

    return list(orders.values())