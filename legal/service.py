import os , json , io
from pdf2image import convert_from_path
from google import genai
from google.genai import types

PROMPT = """
You are an FSSAI label compliance expert for Indian food products.
Carefully examine this product label image and extract the following 20 parameters.
Return ONLY a valid JSON object. No explanation, no preamble, no markdown.
 
For each parameter return:
  - value: what you found (or null if not present)
  - confidence: "high", "medium", or "low"
  - notes: any relevant observation
 
Parameters to extract:
 
1. food_name: The declared name of the food product
2. product_category: Category of the product (e.g. Edible Oil, Beverage)
3. veg_nonveg: Is veg or non-veg symbol present? State: present/absent and color seen
4. ingredients: List all ingredients IN THE ORDER they appear on label (as ordered array)
5. serving_details: Serving size and servings per package
6. nutritional_facts: All nutrients listed IN ORDER they appear (as ordered array with per100g and per serving values)
7. manufacturer_packer_details: Full manufacturer and packer name and address
8. fssai_details: FSSAI logo present? and FSSAI licence number
9. importer_country_of_origin: Importer name if any and country of origin
10. date_of_mfg: Manufacturing and packaging date (may say 'see pack' or similar)
11. expiry_date: Expiry or best before date (may say 'see pack' or similar)
12. batch_lot_number: Batch or lot number (may say 'see pack' or similar)
13. mrp: MRP value including currency (may say 'see pack' or similar)
14. unit_sale_price: Unit sale price if declared
15. barcode: Is a barcode present? What type if identifiable?
16. illustration_disclaimer: Any disclaimer about product illustration or image
17. packaging_epr: Any packaging logo (recycling symbol etc) and EPR compliance declaration
18. jivo_trademark: Any trademark declaration for Jivo brand
19. iso_certification: Any ISO certification mentioned
20. Disclaimer of any signs (* , $ , #) mentioned or not
 
Return format:
{
  "food_name": {"value": "...", "confidence": "high", "notes": "..."},
  "product_category": {"value": "...", "confidence": "high", "notes": "..."},
  ... (all 19 parameters)
}
"""


def run_extraction(pdf_path):
    pages = convert_from_path(
        pdf_path,
        dpi=300,
        poppler_path=r"C:\poppler-26.02.0\Library\bin"  # ← add this line
    )
    
    
    pil_image =  pages[0]
    print(f"Image Size {pil_image.size}")

    img_bytes_io = io.BytesIO()
    pil_image.save(img_bytes_io , format= "JPEG" , quality=95  )
    img_bytes =  img_bytes_io.getvalue()
    
    
    client = genai.Client(api_key = 'AQ.Ab8RN6KaHslkJrADRSiM8znhWZwBQNGO0c1jmqjvyHf9R-49GQ')
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            PROMPT
        ]
    )
    
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
 
    try:
        data = json.loads(raw)
        return data

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw response:\n{raw}")

