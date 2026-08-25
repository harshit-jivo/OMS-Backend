from .services.services import SalesOrderService

# Company databases the invoice queries can be pointed at.
VALID_BRANCHES = ('OIL', 'BEVERAGE')


def normalize_branch(value, default='OIL'):
    """Map whatever the caller sent ('oil', 'bev', 'BEVERAGES', '') to a branch.

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
    return None


def resolve_doc_entry(doc_num, branch='OIL'):
    """DocNum -> OINV."DocEntry" for the given company, or None if not found.

    DocNum is the number printed on the invoice; every downstream consumer
    (Crystal bill print, SAP Service Layer) keys off DocEntry instead, and the
    same DocNum can exist in both company databases — hence the branch.
    """
    rows = SalesOrderService().get_docEntry(doc_num, branch)
    if not rows:
        return None
    return rows[0]['DocEntry']


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