import os , json , io
from pdf2image import convert_from_path
from google import genai
from google.genai import types
from .models import LabelNutrition  
from .serializers import LabelNutritionSerializers

def get_prompt(nutritional_facts_json):
    template = """
    You are an FSSAI label compliance expert for Indian food products.
    Carefully examine this product label image and extract the following 19 parameters.
    Return ONLY a valid JSON object. No explanation, no preamble, no markdown.
    
    For EVERY parameter, return exactly these 4 fields:
      - value: what you found on the label (or null if not present)
      - status: one of "OK", "MISSING", "MISMATCH", "NEEDS_REVIEW", "NOT_APPLICABLE"
      - confidence: "high", "medium", or "low"
      - notes: brief explanation, especially required if status is not "OK"
    
    Status rules to follow:
      - Use "MISSING" when a required field is genuinely absent from the label
      - Use "MISMATCH" when the label value conflicts with reference data provided to you
      - Use "NEEDS_REVIEW" when text is unclear, blurry, or ambiguous
      - Use "NOT_APPLICABLE" only for importer/country_of_origin when product is domestic
      - Use "OK" only when the field is present, clear, and correct
    
    Parameters to extract:
    
    1. food_name: The declared name of the food product
    
    2. product_category: Category of the product (e.g. Edible Oil, Beverage)
    
    3. veg_nonveg: Is veg or non-veg symbol present? State color seen
    
    4. ingredients: List all ingredients IN THE ORDER they appear on label (as ordered array)
    
    5. serving_details: Serving size and servings per package
    
    6. nutritional_facts: All nutrients listed IN ORDER they appear on label, with per_100g 
       and per_serving values exactly as printed. Compare EACH nutrient against this 
       reference data from our database:
       __NUTRITION_DATA__
    
       Return as an ordered array. Each item must include:
       nutrition_name, label_per_100g, label_per_serving, db_per_100g, db_per_serving,
       status ("OK" if values match exactly, "MISMATCH" if they differ, 
       "MISSING" if nutrient is on label but not in database or vice versa), and notes.
    
    7. manufacturer_packer_details: Full manufacturer and packer name and address
    
    8. fssai_details: FSSAI logo present? and FSSAI licence number (must be exactly 14 digits 
       - mark status MISMATCH if not 14 digits)
    
    9. importer_country_of_origin: Importer name if any and country of origin
    
    10. date_of_mfg: Manufacturing and packaging date (may legitimately say 'refer to body')
    
    11. expiry_date: Expiry or best before date (may legitimately say 'refer to body')
    
    12. cost_block: MRP followed by (Incl. of all taxes), then USP (Unit Sale Price) with 
        ₹ per gram/litre unit, then Batch No, then Packing Date, then Use By — in that exact 
        order. Mark status MISMATCH if the order on the label differs from this sequence.
    
    13. barcode: Is a barcode present? What type if identifiable?
    
    14. illustration_disclaimer: Any disclaimer about product illustration or image
    
    15. jivo_trademark: Any trademark declaration for Jivo brand
    
    16. compliance_section: ISO Certification No, followed by EPR Brand Owner Name, followed 
        by Registration Number — in that exact order. Mark status MISMATCH if order differs.
    
    17. footnote_signs: Any reference signs (* , $ , #, ^) used in the nutrition table — 
        confirm each one has a corresponding explanation/footnote on the label. Mark status 
        MISMATCH if a sign is used but not explained.
    
    Return ONLY this JSON structure, with one entry per parameter above:
    {{
      "food_name": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "product_category": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "veg_nonveg": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "ingredients": {{"value": [...], "status": "OK", "confidence": "high", "notes": "..."}},
      "serving_details": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "nutritional_facts": {{"value": [...], "status": "OK", "confidence": "high", "notes": "..."}},
      "manufacturer_packer_details": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "fssai_details": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "importer_country_of_origin": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "date_of_mfg": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "expiry_date": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "cost_block": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "barcode": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "illustration_disclaimer": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "jivo_trademark": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "compliance_section": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}},
      "footnote_signs": {{"value": "...", "status": "OK", "confidence": "high", "notes": "..."}}
    }}
    """
    
    return template.replace("__NUTRITION_DATA__", nutritional_facts_json)

def get_nutritional_facts_json(item_id):
    qs = LabelNutrition.objects.filter(label_item=item_id).values(
        'nutrition_name', 'per_100gm', 'per_serving'
    )
    
    # Convert Decimal to float so json.dumps works
    clean_data = [
        {
            'nutrition_name': row['nutrition_name'],
            'per_100gm': float(row['per_100gm']),
            'per_serving': float(row['per_serving']),
        }
        for row in qs
    ]
    
    return json.dumps(clean_data, indent=2)


def run_extraction(pdf_path , item_id):
    

    nutritional_facts = get_nutritional_facts_json(item_id)
    # print(nutritional_facts)
    PROMPT = get_prompt(nutritional_facts)
    # print(PROMPT)
    
    # print(PROMPT)
    pages = convert_from_path(  
        pdf_path,
        dpi=300,
        poppler_path=r"C:\poppler-26.02.0\Library\bin"  
    )
    
    
    pil_image =  pages[0]
  

    img_bytes_io = io.BytesIO()
    pil_image.save(img_bytes_io , format= "JPEG" , quality=95  )
    img_bytes =  img_bytes_io.getvalue()
    
    
    client = genai.Client(api_key = 'AQ.Ab8RN6JYg9867RSNwakfbAvjjWn0XhPPlHixyfNC7uzbJ3ZIEQ' )
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
        print(f"Parsed JSON:\n{json.dumps(data, indent=2)}")
        return data


    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw response:\n{raw}")

