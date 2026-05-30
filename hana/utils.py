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