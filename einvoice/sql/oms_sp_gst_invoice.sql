-- ============================================================================
--  OMS_SP_GST_INVOICE -- GST A/R invoice print datasource for the OMS Crystal
--  report.  Derived from CRYSTAL_AR_INVOICE_ITEMS (the procedure the .rpt
--  aliases as "UNE_SP_GST_INVOICE").  Signature unchanged: (IN DOCKEY INT).
--
--  ONLY change: "@UTL_MDEXTH" replaced by a ranked UNION of
--      1. OMS_IRN_LOG  (OMS-generated IRNs)  <-- preferred
--      2. @UTL_MDEXTH  (SAP add-on IRNs)     <-- fallback
--  ROW_NUMBER()=1 -> exactly one row per (BaseEntry, DocType).
--  All original output fields are preserved, incl. "LICENSE FSSAI",
--  "Customer Fassai No", "UNE QR Code", "UNE IRN No".
--  Does NOT modify CRYSTAL_AR_INVOICE_ITEMS.
-- ============================================================================

CREATE PROCEDURE "OMS_SP_GST_INVOICE" (IN DOCKEY INT)

LANGUAGE SQLSCRIPT

AS 

Begin

SELECT 

OINV."DocDate", OINV."CardCode", OCRD."U_Main_Group" AS  "Trade",(Select OCRG."GroupName" from OCRG where OCRG."GroupCode"= 113) AS "State",OCRD."U_Chain" AS  "Chain",

CASE WHEN OCRD."GroupCode" = 145 THEN 'Export Invoice' WHEN TO_VARCHAR(OINV."GSTTranTyp") = 'GA' THEN 'TAX INVOICE' WHEN TO_VARCHAR(OINV."GSTTranTyp") = 'GD' THEN 'DEBIT NOTE' ELSE 'BILL OF SUPPLY' END AS "GSTTranTyp",            

INITCAP((SELECT "CompnyName" FROM OADM)) CompnyName, OCRY."Name" AS "CONTRYNAM", IFNULL(TO_VARCHAR(OINV."U_Dipatch_Date", 'DD/MM/YYYY'), NULL) AS "DispatchDate", OINV."DocNum", OCRD."CardName",

Case When "U_AddressIdPrint"='Y' Then (Select A."Address" from CRD1 A Where A."CardCode"=OINV."CardCode" and A."AdresType"='S' and A."Address"=OINV."ShipToCode") Else OINV."CardName" End ShipToName,

Case When "U_AddressIdPrint"='Y' Then (Select A."Address" from CRD1 A Where A."CardCode"=OINV."CardCode" and A."AdresType"='B' and A."Address"=OINV."ShipToCode") Else OINV."CardName" End BillToName,

IFNULL(CRD1."Address2", '') || ' ' || IFNULL(CRD1."Address3", '') || ' ' || IFNULL(CRD1."StreetNo", '') || ' ' || IFNULL(CRD1."Street", '') || '  ' || IFNULL(CRD1."Block", '') || '  ' || IFNULL(CRD1."City", '') || ' - ' || IFNULL((SELECT OCRY."Name" FROM OCRY WHERE OCRY."Code" = CRD1."Country"), '') || '-' || IFNULL(CRD1."ZipCode", '') AS "ADDRESS",

CRD1."Address" AS "ADD1",OINV."NumAtCard",OINV."TrnspCode" AS "SHIPAD",OINV."DocEntry", (TO_VARCHAR(OINV."ObjType") || TO_VARCHAR(OINV."DocEntry")) AS "Scan",INV1."ItemCode",

CASE 

    WHEN OINV."CardCode" IN 

        ('CUSTA000001','CUSTA000002','CUSTA000003','CUSTA000004')

    THEN COALESCE(

        (

            SELECT MIN(T1."FromWhsCod")

            FROM OWTR T0

            INNER JOIN WTR1 T1 ON T1."DocEntry" = T0."DocEntry"

            WHERE T0."DocEntry" = OINV."BaseEntry"

        ),

        INV1."WhsCode"

    )

    ELSE INV1."WhsCode"

END AS "WhsCode"





,

(CASE WHEN INV1."Currency" = 'INR' THEN IFNULL(INV1."LineTotal", 0) ELSE IFNULL(INV1."TotalFrgn", 0) END) AS "VATSUM", OINV."DiscSum",

CASE WHEN OITM."SalFactor3" > 1 THEN INV1."Quantity" * OITM."SalFactor3" ELSE INV1."Quantity" END AS "Quantity", INV1."Quantity" / OITM."SalFactor2" AS "Box", OITM."SalFactor3" AS "Box1",

CASE WHEN OITM."SalFactor3" > 1 THEN INV1."Quantity" * OITM."SalFactor3" * OITM."U_Gross_Weight" ELSE INV1."Quantity" * OITM."U_Gross_Weight" END AS "Gross_Weights",

CASE WHEN OITM."SalFactor3" > 1 THEN INV1."Quantity" WHEN OITM."SalFactor2" = 1 THEN 0 ELSE CAST(INV1."Quantity" / OITM."SalFactor2" AS INTEGER) END AS "BoxInt",

CASE WHEN OITM."SalFactor2" != 1 THEN CAST((INV1."Quantity" - CAST((INV1."Quantity" / OITM."SalFactor2") AS INTEGER) * OITM."SalFactor2") AS INTEGER) WHEN OITM."SalPackMsr" IN ('Drum') THEN 0 WHEN OITM."SalFactor3" != 1 THEN 0 ELSE (INV1."Quantity") END AS "LooseQty", OITM."SalPackMsr" AS "UOM",

CASE WHEN OITM."SalFactor2" = 1 THEN 'Per Piece' ELSE 'Per Box' END AS "UOM1", IFNULL(OITM."SalPackMsr", OITM."SalUnitMsr") AS "LooseUOM", CAST((INV1."Quantity" - CAST((INV1."Quantity" / OITM."SalFactor2") AS INTEGER) * OITM."SalFactor2") AS INTEGER) AS "Loose",

CASE WHEN OINV."DocCur" = 'INR' THEN OINV."DocTotal"  ELSE OINV."DocTotalFC" END AS "DOCTOTAL",'Null' as "Realise",

      CASE

         WHEN OITM."Series" = 109 AND OITM."ItmsGrpCod" = '105' THEN 'Packing Material-'

         WHEN OITM."Series" = 109 AND OITM."ItmsGrpCod" = '106' THEN 'Raw Material-'

         WHEN OITM."Series" = 107 AND OITM."ItmsGrpCod" = '107' THEN ' Scheme-'

         WHEN OITM."Series" = 108 THEN 'Raw Material-'

         ELSE ''

      END AS "ItmsGroup",INV1."Dscription" AS  "DSCRIPTION",

      IFNULL(INV1."FreeTxt", '') AS "FreeText",INV1."Price" AS "PRICE",CASE WHEN OITM."SalFactor2" != 1 THEN INV1."Price" * OITM."SalFactor2"ELSE INV1."Price" 

      END AS "NetPricePerUom", INV1."DiscPrcnt",INV1."PriceBefDi",

      CASE  WHEN OITM."SalFactor2" != 1 THEN INV1."PriceBefDi" * OITM."SalFactor2" ELSE INV1."PriceBefDi" END AS "PricePerUom",

      CASE WHEN OITM."SalFactor2" != 1 THEN TO_VARCHAR(CAST(OITM."SalFactor2" AS INTEGER)) ELSE '' END AS "BoxSize",

     CASE WHEN INV1."Currency" = 'INR' THEN IFNULL(INV1."LineTotal", 0)  ELSE IFNULL(INV1."TotalFrgn", 0) END AS "LINETOTAL",

    --(Select Name from [@TRANSPORTERLIST] L Where L.Code=OINV.U_TransporterName) TransporterName,

     IFNULL( (SELECT IFNULL(INV4."TaxRate", 0) FROM "INV4" WHERE INV4."DocEntry" = INV1."DocEntry" AND INV4."staType" IN ('-100') AND INV4."RelateType" = 1  AND INV1."LineNum" = INV4."LineNum"),  0) AS "CGSTRATE",

      IFNULL(

    (SELECT CASE 

        WHEN SUM(INV4."RvsChrgPrc") != 0 

        THEN (

            (CASE 

                WHEN OINV."DocCur" = 'INR' 

                THEN SUM(INV4."TaxSum") 

                ELSE SUM(INV4."TaxSumFrgn") 

             END) - 

            (CASE 

                WHEN OINV."DocCur" = 'INR' 

                THEN SUM(INV4."RvsChrgTax") 

                ELSE SUM(INV4."RvsChrgFC") 

             END)

        )

        ELSE (CASE 

            WHEN OINV."DocCur" = 'INR' 

            THEN SUM(INV4."TaxSum") 

            ELSE SUM(INV4."TaxSumFrgn") 

        END)

     END 

     FROM "INV4"

     WHERE INV4."DocEntry" = INV1."DocEntry" 

       AND INV4."staType" = '-100' 

       AND INV4."LineNum" = INV1."LineNum" 

       AND INV4."RelateType" IN (1)

    ), 0

) +

IFNULL(

    (SELECT CASE 

        WHEN SUM(INV4."RvsChrgPrc") != 0 

        THEN (

            (CASE 

                WHEN OINV."DocCur" = 'INR' 

                THEN SUM(INV4."TaxSum") 

                ELSE SUM(INV4."TaxSumFrgn") 

             END) - 

            (CASE 

                WHEN OINV."DocCur" = 'INR' 

                THEN SUM(INV4."RvsChrgTax") 

                ELSE SUM(INV4."RvsChrgFC") 

             END)

        )

        ELSE (CASE 

            WHEN OINV."DocCur" = 'INR' 

            THEN SUM(INV4."TaxSum") 

            ELSE SUM(INV4."TaxSumFrgn") 

        END)

     END 

     FROM "INV4"

     WHERE INV4."DocEntry" = INV1."DocEntry" 

       AND INV4."staType" = '-100'

       AND INV4."RelateType" IN (3)

    ), 0

) AS "CGSTAMNT",

 IFNULL((SELECT IFNULL(INV4."TaxRate", 0) FROM "INV4" WHERE INV4."DocEntry" = INV1."DocEntry" AND INV4."staType" IN ('-110', '-150') AND INV4."RelateType" = 1   AND INV1."LineNum" = INV4."LineNum"),  0) AS "SGSTRATE",

       IFNULL(

    (SELECT CASE 

        WHEN SUM(INV4."RvsChrgPrc") != 0 

        THEN (

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END) - 

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."RvsChrgTax") ELSE SUM(INV4."RvsChrgFC") END)

        )

        ELSE (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END)

     END

     FROM "INV4"

     WHERE INV4."DocEntry" = INV1."DocEntry"

       AND INV4."staType" IN ('-110', '-150') 

       AND INV4."LineNum" = INV1."LineNum"

       AND INV4."RelateType" IN (1)

    ), 0

) +

IFNULL(

    (SELECT CASE 

        WHEN SUM(INV4."RvsChrgPrc") != 0 

        THEN (

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END) - 

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."RvsChrgTax") ELSE SUM(INV4."RvsChrgFC") END)

        )

        ELSE (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END)

     END

     FROM "INV4"

     WHERE INV4."DocEntry" = INV1."DocEntry"

       AND INV4."staType" IN ('-110', '-150')

       AND INV4."RelateType" IN (3)

    ), 0

) AS "SGSTAMNT",



    IFNULL(

    (SELECT IFNULL(INV4."TaxRate", 0) FROM "INV4" WHERE INV4."DocEntry" = INV1."DocEntry"

       AND INV4."staType" IN ('-120')   AND INV4."RelateType" = 1   AND INV4."LineNum" = INV1."LineNum" ), 0) AS "IGSTRATE",

       

       IFNULL(

    (SELECT CASE 

        WHEN SUM(INV4."RvsChrgPrc") != 0 

        THEN (

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END) - 

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."RvsChrgTax") ELSE SUM(INV4."RvsChrgFC") END)

        )

        ELSE (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END)

     END

     FROM "INV4"

     WHERE INV4."DocEntry" = INV1."DocEntry"

       AND INV4."staType" = '-120'

       AND INV4."LineNum" = INV1."LineNum"

       AND INV4."RelateType" IN (1)

    ), 0

) +

IFNULL(

    (SELECT CASE 

        WHEN SUM(INV4."RvsChrgPrc") != 0 

        THEN (

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END) - 

            (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."RvsChrgTax") ELSE SUM(INV4."RvsChrgFC") END)

        )

        ELSE (CASE WHEN OINV."DocCur" = 'INR' THEN SUM(INV4."TaxSum") ELSE SUM(INV4."TaxSumFrgn") END)

     END

     FROM "INV4"

     WHERE INV4."DocEntry" = INV1."DocEntry"

       AND INV4."staType" = '-120'

       --AND INV4."LineNum" = INV1."LineNum"

       AND INV4."RelateType" IN (3)

    ), 0

) AS "IGSTAMNT",



IFNULL(

    (SELECT T2."Name" 

     FROM "CRD1" T1 

     INNER JOIN "OCST" T2 ON T2."Code" = T1."State" 

     WHERE T1."CardCode" = OINV."CardCode" 

       AND T1."AdresType" = 'S' 

       AND T1."Address" = OINV."ShipToCode" 

       AND T2."Country" = T1."Country"), 

    ''

) AS "STATE 1",



---- Reverse Charges

IFNULL(

    (SELECT CASE 

        WHEN OINV."DocCur" <> 'INR' 

        THEN SUM(INV4."RvsChrgFC") 

        ELSE SUM(INV4."RvsChrgPrc") 

    END 

    FROM "INV4" 

    WHERE INV4."DocEntry" = INV1."DocEntry"), 

    0

) AS "Reverse Charges",

'0' AS "Reverse SGST Tax",

'0' AS "Reverse SGST Tax",



--(CASE 

   -- WHEN INV1."Currency" = 'INR' 

   -- THEN IFNULL((SELECT SUM(INV4."RvsChrgTax") 

                -- FROM "INV4" 

                -- WHERE INV4."DocEntry" = INV1."DocEntry" 

                   --AND INV4."staType" IN ('-120')), 0)

    --ELSE IFNULL((SELECT SUM(INV4."RvsChrgFC") 

                -- FROM "INV4" 

                 --WHERE INV4."DocEntry" = INV1."DocEntry" 

                 --  AND INV4."staType" IN ('-120')), 0)

--END) AS "Reverse IGST Tax",

'' AS "Reverse IGST Tax",



(SELECT OCHP."ChapterID" FROM "OCHP" WHERE OCHP."AbsEntry" = OITM."ChapterID") AS "TARIFF HEADING",

(SELECT OCRN."DocCurrCod" FROM "OCRN"  WHERE OCRN."CurrCode" = OINV."DocCur") AS "DocCur",

CRD7."TaxId0" AS "CustPAN",OLCT."PanNo" AS "CmpnyPAN",OLCT."GSTRegnNo" AS "LOCTGSTREGNO",

(SELECT P."GSTRegnNo" FROM "CRD1" P WHERE P."CardCode" = OINV."CardCode"  AND P."Address" = OINV."ShipToCode" AND P."AdresType" = 'S') AS "GSTREGNNO",

(SELECT P."GSTRegnNo" FROM "CRD1" P WHERE P."CardCode" = OINV."CardCode"  AND P."Address" = OINV."PayToCode" AND P."AdresType" = 'B') AS "Bill To GSTREGNNO",

--OCST."GSTCode" ,

'' as "GSTCode",

INV1."unitMsr",



CASE 

    WHEN OINV."U_Ship_From" IS NULL AND INV1."WhsCode" NOT IN ('PB-JP', 'PB-ST', 'PB-SP','PB-RG','GP-FG','BH-LR') THEN

        INITCAP(

            IFNULL(OLCT."Street", '') || ' ' || 

            IFNULL(OLCT."Block", '') || ' ' ||

            IFNULL(OLCT."Building", '') || ' ' ||  

            IFNULL(OLCT."City", '') || ' - ' ||

            IFNULL((SELECT OCST."Name" FROM OCST WHERE OCST."Code" = OLCT."State" AND OCST."Country" = OLCT."Country"), '') || '-' || 

            IFNULL((SELECT OCRY."Name" FROM OCRY WHERE OCRY."Code" = OLCT."Country"), '') || '-' || 

            IFNULL(OLCT."ZipCode", '')

        )



    WHEN INV1."WhsCode" IN ('PB-JP', 'PB-ST', 'PB-SP','PB-RG','GP-FG','BH-LR') THEN 

        INITCAP(

            IFNULL(OWHS."AddrType", '') || ' ' ||

            IFNULL(OWHS."Street", '') || ' ' ||

            IFNULL(OWHS."StreetNo", '') || ' ' ||

            IFNULL(OWHS."Block", '') || ' ' ||

            IFNULL(OWHS."Building", '') || ' ' ||

            IFNULL(OWHS."City", '') || ' ' ||

            IFNULL(OWHS."County", '') || ' ' ||

           -- IFNULL(OWHS."State", '') || ' ' ||

            IFNULL(OWHS."ZipCode", '') --|| ' ' ||

           -- IFNULL(OWHS."Country", '')

        )



    WHEN OINV."U_Ship_From" = 'DL-PS' THEN

        INITCAP(

            IFNULL(OLCT."Street", '') || ' ' || 

            IFNULL(OLCT."Block", '') || ' ' ||

            IFNULL(OLCT."Building", '') || ' ' ||  

            IFNULL(OLCT."City", '') || ' - ' ||

            IFNULL((SELECT OCST."Name" FROM OCST WHERE OCST."Code" = OLCT."State" AND OCST."Country" = OLCT."Country"), '') || '-' || 

            IFNULL((SELECT OCRY."Name" FROM OCRY WHERE OCRY."Code" = OLCT."Country"), '') || '-' || 

            IFNULL(OLCT."ZipCode", '')

        )



    ELSE 

        INITCAP(

            (SELECT 

                IFNULL(K."AddrType", '') || ' ' ||

                IFNULL(K."Street", '') || ' ' ||

                IFNULL(K."StreetNo", '') || ' ' ||

                IFNULL(K."Block", '') || ' ' ||

                IFNULL(K."City", '') || ' ' ||

                IFNULL(K."ZipCode", '') || ' ' ||

                IFNULL((SELECT Y."Name" FROM OCST Y WHERE Y."Code" = K."State" AND Y."Country" = 'IN'), '') || ' ' ||

                IFNULL((CASE WHEN K."Country" = 'IN' THEN 'India' ELSE K."Country" END), '')

            FROM OWHS K WHERE K."WhsCode" = OINV."U_Ship_From")

        )

END AS "Company Address",





IFNULL((SELECT OCST."Name" 

        FROM OCST 

        WHERE OCST."Code" = CRD1."State" 

        AND OCST."Country" = CRD1."Country" 

        AND CRD1."Address" = OINV."ShipToCode"), '') AS "STATE2",

OINV."DocCur",

CASE 

    WHEN IFNULL(INV1."DiscPrcnt", 0) = 0 THEN 0 

    ELSE 

        ((CASE 

            WHEN IFNULL(INV1."Quantity", 0) = 0 THEN 1 

            ELSE IFNULL(INV1."Quantity", 0) 

        END) * 

        (CASE 

            WHEN OINV."DocCur" = 'INR' THEN IFNULL(INV1."PriceBefDi", 0) 

            ELSE IFNULL(INV1."PriceBefDi", 0) 

        END * IFNULL(INV1."DiscPrcnt", 0) / 100)) 

END AS "Disc Amt",

OINV."DiscPrcnt" AS "Header Disc Percent",

CASE 

    WHEN OINV."DocCur" = 'INR' THEN OINV."DiscSum" 

    ELSE OINV."DiscSumFC" 

END AS "Header Disc Amt",

CASE 

    WHEN OINV."DocCur" = 'INR' THEN OINV."RoundDif" 

    ELSE OINV."RoundDifFC" 

END AS "RoundDif",OINV."RevRefDate" AS "Original Ref. Date",OINV."RevRefNo" AS "Original Ref. No.",IFNULL(OINV."Reserve", 'N') AS "Reserve",OINV."Comments",

--INV1."U_Remarks" AS "Remarks",

OINV."TrackNo" AS "Pac_Slip",

(CASE 

    WHEN OINV."DocCur" = 'INR' THEN IFNULL(INV1."LineTotal", 0) 

    ELSE IFNULL(INV1."TotalFrgn", 0) 

END) AS "Taxable Value",



CASE 

    WHEN OINV."DocCur" = 'INR' THEN OINV."DpmAmnt" 

    ELSE OINV."DpmAmntFC" 

END AS "Total Down Payment",

OINV."DocDueDate" AS "Due Date",



-- Payment Terms

(SELECT OCTG."PymntGroup"   

 FROM OCTG 

 WHERE OCTG."GroupNum" = OCRD."GroupNum") AS "Payment Terms",



-- Contact Information

(SELECT MIN(OCPR."Name")  

 FROM OCPR 

 WHERE OCPR."CardCode" = CRD1."CardCode") AS "Contact Name",

(SELECT MIN(OCPR."Cellolar")  

 FROM OCPR 

 WHERE OCPR."CardCode" = CRD1."CardCode") AS "Contact Mobile",

(SELECT MIN(OCPR."E_MailL")  

 FROM OCPR 

 WHERE OCPR."CardCode" = CRD1."CardCode") AS "Contact Email Id",



OINV."NumAtCard" AS "Ref No",

IFNULL((SELECT INV4."StaCode" 

        FROM INV4 

        WHERE INV4."DocEntry" = OINV."DocEntry" 

        AND INV4."staType" = '-110' 

        AND INV4."RelateType" = 1 

        AND INV4."LineNum" = (SELECT MIN(Inv4Sub."LineNum") 

                            FROM INV4 AS Inv4Sub 

                            WHERE Inv4Sub."DocEntry" = OINV."DocEntry" 

                            AND Inv4Sub."staType" = '-110' 

                            AND Inv4Sub."RelateType" = 1)

       ), '') AS "SGST",



IFNULL((SELECT INV4."StaCode" 

        FROM INV4 

        WHERE INV4."DocEntry" = OINV."DocEntry" 

        AND INV4."staType" = '-150' 

        AND INV4."RelateType" = 1 

        AND INV4."LineNum" = (SELECT MIN(Inv4Sub."LineNum")  

                            FROM INV4 AS Inv4Sub 

                            WHERE Inv4Sub."DocEntry"  = OINV."DocEntry"  

                            AND Inv4Sub."staType" = '-150' 

                            AND Inv4Sub."RelateType" = 1)

       ), '') AS "UGST",

       CASE WHEN (SELECT A."Building" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B'  AND A."Address" = OINV."PayToCode") IS NULL THEN '' ELSE CAST((SELECT A."Building" FROM "CRD1" A   WHERE A."CardCode" = OINV."CardCode"   AND A."AdresType" = 'B'  AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', ' END ||

CASE  WHEN (SELECT A."StreetNo" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B'  AND A."Address" = OINV."PayToCode") IS NULL THEN '' ELSE CAST((SELECT A."StreetNo" FROM "CRD1" A   WHERE A."CardCode" = OINV."CardCode"  AND A."AdresType" = 'B'  AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', '  END ||

CASE  WHEN (SELECT A."Address2" FROM "CRD1" A   WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B'   AND A."Address" = OINV."PayToCode") IS NULL THEN '' ELSE CAST((SELECT A."Address2" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode"  AND A."AdresType" = 'B'  AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Address3" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode"  AND A."AdresType" = 'B'   AND A."Address" = OINV."PayToCode") IS NULL THEN '' ELSE CAST((SELECT A."Address3" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B'    AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Street" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode"   AND A."AdresType" = 'B' AND A."Address" = OINV."PayToCode") IS NULL  THEN '' ELSE CAST((SELECT A."Street" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode"  AND A."AdresType" = 'B' AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Block" FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B'   AND A."Address" = OINV."PayToCode") IS NULL  THEN ''   ELSE CAST((SELECT A."Block" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B'  AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."City" FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode"   AND A."AdresType" = 'B' AND A."Address" = OINV."PayToCode") IS NULL THEN '' ELSE CAST((SELECT A."City" FROM "CRD1" A   WHERE A."CardCode" = OINV."CardCode"  AND A."AdresType" = 'B'  AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."State" FROM "CRD1" A  WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B' AND A."Address" = OINV."PayToCode") IS NULL THEN '' ELSE CAST((SELECT A."State" FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'B'      AND A."Address" = OINV."PayToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Country" FROM "CRD1" A   WHERE A."CardCode" = OINV."CardCode"   AND A."AdresType" = 'B' AND A."Address" = OINV."PayToCode") IS NULL THEN '' ELSE CAST((SELECT A."Country" FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode"  AND A."AdresType" = 'B'   AND A."Address" = OINV."PayToCode") AS NVARCHAR) 

END AS "BILL TO Address",



CASE WHEN (SELECT A."Building"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."Building"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."StreetNo"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."StreetNo"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Address2"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."Address2"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Address3"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."Address3"  FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Street"    FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."Street"    FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Block"     FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."Block"     FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."City"      FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."City"      FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."State"     FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT (Select KK."Name" from OCST KK Where KK."Code"=A."State" and KK."Country"=A."Country") from CRD1 A Where A."CardCode"=OINV."CardCode" and "AdresType"='S' and A."Address"=OINV."ShipToCode") as nvarchar) ||', ' END||

CASE WHEN (SELECT A."ZipCode"   FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT A."ZipCode"   FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S'  AND A."Address" = OINV."ShipToCode") AS NVARCHAR) || ', ' END ||

CASE WHEN (SELECT A."Country"   FROM "CRD1" A WHERE A."CardCode" = OINV."CardCode" AND A."AdresType" = 'S' AND A."Address" = OINV."ShipToCode") IS NULL THEN '' ELSE CAST((SELECT (SELECT "Name" FROM "OCRY" J  Where J."Code"= K."Country") from "CRD1" K Where K."CardCode"=OINV."CardCode" and K."AdresType"='S' and K."Address"=OINV."ShipToCode") as nvarchar)

END AS "SHIP TO",



IFNULL((SELECT T2."Name" FROM CRD1 T1 INNER JOIN OCST T2 ON T2."Code" = T1."State" WHERE T1."CardCode" = OINV."CardCode" AND T1."AdresType" = 'B' 

       AND T1."Address" = OINV."PayToCode" AND T2."Country" = T1."Country"), '') AS "Bill To State",

       

IFNULL((SELECT T2."GSTCode" FROM CRD1 T1 INNER JOIN OCST T2 ON T2."Code" = T1."State" WHERE T1."CardCode" = OINV."CardCode" AND T1."AdresType" = 'S' 

       AND T1."Address" = OINV."ShipToCode" AND T2."Country" = T1."Country"), '') AS "GST CODE SHIP",

       OINV."BaseType",

IFNULL((SELECT T2."GSTCode" FROM CRD1 T1 INNER JOIN OCST T2 ON T2."Code" = T1."State" WHERE T1."CardCode" = OINV."CardCode" AND T1."AdresType" = 'B' 

       AND T1."Address" = OINV."PayToCode" AND T2."Country" = T1."Country"),'') AS "GST CODE",





-----------------------------------------------------------------------------------------------------------------------------------------TO MAIN HANA

IFNULL((SELECT YY."BatchNum" FROM (SELECT T0."BatchNum",

 --RANK() OVER (ORDER BY T0."BatchNum") AS "RowNum" 

        ROW_NUMBER() OVER (ORDER BY T0."BatchNum") AS "RowNum"

        FROM "OIBT" T0

        INNER JOIN "IBT1" T1 ON T0."ItemCode" = T1."ItemCode" AND T0."WhsCode" = T1."WhsCode"  AND T0."BatchNum" = T1."BatchNum"

        INNER JOIN "OITM" T2 ON T0."ItemCode" = T2."ItemCode" WHERE T1."BaseType" = '13' AND T1."BaseEntry" = "INV1"."DocEntry"

        AND T1."ItemCode" = "INV1"."ItemCode" AND T1."BaseLinNum" = "INV1"."LineNum") YY WHERE "RowNum" = 1), '') AS "BatchNum",

--------------------------------------------------------------------------------------------------------------------------------------------

--IFNULL(CAST(CAST((SELECT (CASE WHEN T0."PrdDate" IS NULL THEN T0."InDate" ELSE T0."PrdDate" END) FROM "OIBT" T0

--        INNER JOIN "IBT1" T1 ON T0."ItemCode" = T1."ItemCode" AND T0."WhsCode" = T1."WhsCode" AND T0."BatchNum" = T1."BatchNum" 

--        INNER JOIN "OITM" T2 ON T0."ItemCode" = T2."ItemCode" WHERE T1."BaseType" = '13' AND T1."BaseEntry" = "INV1"."DocEntry" 

--        AND T1."ItemCode" = "INV1"."ItemCode" AND T1."BaseLinNum" = "INV1"."LineNum") AS DATE) AS NVARCHAR), '') AS "MFGDate"

(SELECT A."SlpName" 

 FROM "OSLP" A 

 WHERE A."SlpCode" = "OINV"."SlpCode") AS "AuthPName",

 (SELECT A."Memo" 

 FROM "OSLP" A 

 WHERE A."SlpCode" = "OINV"."SlpCode") AS "AuthDesName",

 IFNULL(

    (SELECT SUM("LineTotal") 

     FROM "INV3" 

     WHERE "DocEntry" = "OINV"."DocEntry" 

     AND "ExpnsCode" = 6), 

    0

) AS "Freight",

IFNULL(

    (SELECT SUM("LineTotal") 

     FROM "INV3" 

     WHERE "DocEntry" = "OINV"."DocEntry" 

     AND "ExpnsCode" = 3), 

    0

) AS "Freight Extra",

CURRENT_DATE AS "Datemade",

CASE 

    WHEN (Select K."GSTRegnNo" FROM CRD1 K Where K."CardCode"=OINV."CardCode" and K."AdresType"='B' and K."Address"=OINV."PayToCode") IS NOT NULL AND "OINV"."DocTotal" != 0

                    AND NOT EXISTS (SELECT 1 FROM (SELECT Y."U_UTL_QRPT", Y."U_UTL_IRN", Y."U_UTL_AckNo", Y."U_UTL_IRNGENDT",
                Y."U_UTL_IST", Y."U_UTL_BaseEntry", Y."U_UTL_DocType"
           FROM (SELECT X."U_UTL_QRPT", X."U_UTL_IRN", X."U_UTL_AckNo", X."U_UTL_IRNGENDT",
                        X."U_UTL_IST", X."U_UTL_BaseEntry", X."U_UTL_DocType",
                        ROW_NUMBER() OVER (PARTITION BY X."U_UTL_BaseEntry", X."U_UTL_DocType"
                                           ORDER BY X."SRC_PRIO", X."U_UTL_IRNGENDT" DESC) AS "RN"
                   FROM (SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 1 AS "SRC_PRIO"
                           FROM "OMS_IRN_LOG"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("Canceled",'N') <> 'Y'
                            AND IFNULL("U_UTL_QRPT",'') <> ''
                         UNION ALL
                         SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 2 AS "SRC_PRIO"
                           FROM "@UTL_MDEXTH"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("U_UTL_QRPT",'') <> '') X) Y
          WHERE Y."RN" = 1) AA WHERE AA."U_UTL_DocType" = 13 AND AA."U_UTL_BaseEntry" = "OINV"."DocEntry"

                    AND AA."U_UTL_IST" = 'S' AND IFNULL(AA."U_UTL_QRPT", '') <> '' AND AA."U_UTL_IRN" IS NOT NULL)

    THEN 'Blank'

    ELSE '' END AS "Blank for Billing",

    '0' as "Veh.No",

'0' as "E-Way Bill No",

CASE

WHEN OINV."CardCode" IN ('CUSTA000606','CUSTA000680','CUSTA001061','CUSTA000673','CUSTA000354','CUSTA000844') 

         AND (CRD1."Address" LIKE 'BARU SAHIB' 

              OR CRD1."Address" LIKE 'R%K WORLD%INFOCOM%' 

              OR CRD1."Address" LIKE '%KALGIDHAR%' 

              OR CRD1."Address" LIKE '%ZOMATO%' 

              OR CRD1."Address" LIKE '%NILE%'

              OR CRD1."Address"  LIKE '%JIVOMART%'

              OR CRD1."Address"  LIKE '%B S A%'

              OR CRD1."Address"  LIKE '%CITY MALL%'

              OR CRD1."Address" like 'CHIRAG%'

              OR CRD1."Address" like 'CMUNITY INNOVATIONS PVT LTD GAUTAM BUDH%'

              OR CRD1."Address" like 'SAHIL TRADING CO GANGANAGAR%'

              OR CRD1."Address" like 'STAR SALES%'

              OR CRD1."Address" like 'SCOOTSY LOGISTICS%'

              OR CRD1."Address" like 'THE KALGIDHAR SOCIETY%'

              OR CRD1."Address"='CONNEDIT BUSINESS SOLUTIONS PVT. LTD BIHAR')

    THEN CRD1."Address"

 

    

    ELSE OINV."CardName" 

END AS "CARD_CODE_SHIP",



CASE WHEN "OINV"."BPLId" = 2 AND "INV1"."WhsCode" != 'BH-LR' THEN '10015064000541'

     WHEN "OINV"."BPLId" = 2 AND "INV1"."WhsCode" = 'BH-LR' THEN '10824999000237'

     WHEN "OINV"."BPLId" = 1 THEN '13322999001306'

     WHEN "OINV"."BPLId" = 3 THEN '12123999000082'

     ELSE '10014011001626'

     END AS "LICENSE FSSAI",

     "OCRD"."U_Fssai" as "Customer Fassai No",

 

(Select distinct AA."U_UTL_QRPT" from (SELECT Y."U_UTL_QRPT", Y."U_UTL_IRN", Y."U_UTL_AckNo", Y."U_UTL_IRNGENDT",
                Y."U_UTL_IST", Y."U_UTL_BaseEntry", Y."U_UTL_DocType"
           FROM (SELECT X."U_UTL_QRPT", X."U_UTL_IRN", X."U_UTL_AckNo", X."U_UTL_IRNGENDT",
                        X."U_UTL_IST", X."U_UTL_BaseEntry", X."U_UTL_DocType",
                        ROW_NUMBER() OVER (PARTITION BY X."U_UTL_BaseEntry", X."U_UTL_DocType"
                                           ORDER BY X."SRC_PRIO", X."U_UTL_IRNGENDT" DESC) AS "RN"
                   FROM (SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 1 AS "SRC_PRIO"
                           FROM "OMS_IRN_LOG"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("Canceled",'N') <> 'Y'
                            AND IFNULL("U_UTL_QRPT",'') <> ''
                         UNION ALL
                         SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 2 AS "SRC_PRIO"
                           FROM "@UTL_MDEXTH"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("U_UTL_QRPT",'') <> '') X) Y
          WHERE Y."RN" = 1) AA where AA."U_UTL_BaseEntry" = OINV."DocEntry" and AA."U_UTL_IST" ='S' AND IFNULL(AA."U_UTL_QRPT",'')<>''  AND "U_UTL_DocType"=13 group by AA."U_UTL_QRPT") "UNE QR Code"

,(Select distinct AA."U_UTL_IRN" from (SELECT Y."U_UTL_QRPT", Y."U_UTL_IRN", Y."U_UTL_AckNo", Y."U_UTL_IRNGENDT",
                Y."U_UTL_IST", Y."U_UTL_BaseEntry", Y."U_UTL_DocType"
           FROM (SELECT X."U_UTL_QRPT", X."U_UTL_IRN", X."U_UTL_AckNo", X."U_UTL_IRNGENDT",
                        X."U_UTL_IST", X."U_UTL_BaseEntry", X."U_UTL_DocType",
                        ROW_NUMBER() OVER (PARTITION BY X."U_UTL_BaseEntry", X."U_UTL_DocType"
                                           ORDER BY X."SRC_PRIO", X."U_UTL_IRNGENDT" DESC) AS "RN"
                   FROM (SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 1 AS "SRC_PRIO"
                           FROM "OMS_IRN_LOG"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("Canceled",'N') <> 'Y'
                            AND IFNULL("U_UTL_QRPT",'') <> ''
                         UNION ALL
                         SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 2 AS "SRC_PRIO"
                           FROM "@UTL_MDEXTH"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("U_UTL_QRPT",'') <> '') X) Y
          WHERE Y."RN" = 1) AA where AA."U_UTL_BaseEntry" = OINV."DocEntry" and AA."U_UTL_IST" ='S' AND IFNULL (AA."U_UTL_QRPT",'')<>''  AND "U_UTL_DocType"=13 group by AA."U_UTL_IRN") "UNE IRN No"

,(Select Distinct AA."U_UTL_AckNo" from (SELECT Y."U_UTL_QRPT", Y."U_UTL_IRN", Y."U_UTL_AckNo", Y."U_UTL_IRNGENDT",
                Y."U_UTL_IST", Y."U_UTL_BaseEntry", Y."U_UTL_DocType"
           FROM (SELECT X."U_UTL_QRPT", X."U_UTL_IRN", X."U_UTL_AckNo", X."U_UTL_IRNGENDT",
                        X."U_UTL_IST", X."U_UTL_BaseEntry", X."U_UTL_DocType",
                        ROW_NUMBER() OVER (PARTITION BY X."U_UTL_BaseEntry", X."U_UTL_DocType"
                                           ORDER BY X."SRC_PRIO", X."U_UTL_IRNGENDT" DESC) AS "RN"
                   FROM (SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 1 AS "SRC_PRIO"
                           FROM "OMS_IRN_LOG"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("Canceled",'N') <> 'Y'
                            AND IFNULL("U_UTL_QRPT",'') <> ''
                         UNION ALL
                         SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 2 AS "SRC_PRIO"
                           FROM "@UTL_MDEXTH"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("U_UTL_QRPT",'') <> '') X) Y
          WHERE Y."RN" = 1) AA where AA."U_UTL_BaseEntry" = OINV."DocEntry" and AA."U_UTL_IST" ='S' AND IFNULL (AA."U_UTL_QRPT",'')<>''   AND "U_UTL_DocType"=13 group by AA."U_UTL_AckNo") "UNE Ack no"

,(Select Distinct AA."U_UTL_IRNGENDT" from (SELECT Y."U_UTL_QRPT", Y."U_UTL_IRN", Y."U_UTL_AckNo", Y."U_UTL_IRNGENDT",
                Y."U_UTL_IST", Y."U_UTL_BaseEntry", Y."U_UTL_DocType"
           FROM (SELECT X."U_UTL_QRPT", X."U_UTL_IRN", X."U_UTL_AckNo", X."U_UTL_IRNGENDT",
                        X."U_UTL_IST", X."U_UTL_BaseEntry", X."U_UTL_DocType",
                        ROW_NUMBER() OVER (PARTITION BY X."U_UTL_BaseEntry", X."U_UTL_DocType"
                                           ORDER BY X."SRC_PRIO", X."U_UTL_IRNGENDT" DESC) AS "RN"
                   FROM (SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 1 AS "SRC_PRIO"
                           FROM "OMS_IRN_LOG"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("Canceled",'N') <> 'Y'
                            AND IFNULL("U_UTL_QRPT",'') <> ''
                         UNION ALL
                         SELECT "U_UTL_QRPT","U_UTL_IRN","U_UTL_AckNo","U_UTL_IRNGENDT",
                                "U_UTL_IST","U_UTL_BaseEntry","U_UTL_DocType", 2 AS "SRC_PRIO"
                           FROM "@UTL_MDEXTH"
                          WHERE "U_UTL_IST" = 'S' AND IFNULL("U_UTL_QRPT",'') <> '') X) Y
          WHERE Y."RN" = 1) AA where AA."U_UTL_BaseEntry" = OINV."DocEntry" and AA."U_UTL_IST" ='S' AND IFNULL (AA."U_UTL_QRPT",'')<>''  AND "U_UTL_DocType"=13 group by AA."U_UTL_IRNGENDT") "UNE Ack dt"

 



FROM     

    "OINV" 

    INNER JOIN "INV1" ON "OINV"."DocEntry" = "INV1"."DocEntry"   

    INNER JOIN "OCRD" ON "OINV"."CardCode" = "OCRD"."CardCode"  

    LEFT JOIN "CRD1" ON "OINV"."CardCode" = "CRD1"."CardCode" AND "OINV"."ShipToCode" = "CRD1"."Address" AND "CRD1"."AdresType" = 'S'   

    LEFT JOIN "CRD7" ON "OCRD"."CardCode" = "CRD7"."CardCode" AND "CRD7"."AddrType" = 'S' AND "CRD7"."Address" = "OINV"."ShipToCode"  

    INNER JOIN "OITM" ON "OITM"."ItemCode" = "INV1"."ItemCode"

    LEFT JOIN "OWTR" ON "OWTR"."DocEntry" = "OINV"."BaseEntry" AND "OINV"."BaseType" = 67 

    LEFT JOIN "WTR1" ON "WTR1"."DocEntry" = "OWTR"."DocEntry" AND "INV1"."ItemCode" = "WTR1"."ItemCode" AND "INV1"."LineNum" = "WTR1"."LineNum"

    --LEFT JOIN "OSHP" ON "OSHP"."TrnspCode" = "OINV"."TrnspCode" 

    INNER JOIN "OLCT" ON "OLCT"."Code" = "INV1"."LocCode"   

  --  INNER JOIN "OCST" ON "OCST"."Code" = "CRD1"."State" AND "OCST"."Country" = 'IN'

    LEFT JOIN "OCRY" ON "OCRY"."Code" = "CRD1"."Country" 

    LEFT JOIN OWHS ON "INV1"."WhsCode" = "OWHS"."WhsCode"  

    --INNER JOIN "OUSR" ON "OINV"."UserSign2" = "OUSR"."USERID"

 WHERE "OINV"."DocEntry" = :DocKey  AND "INV1"."TreeType" != 'I';

END