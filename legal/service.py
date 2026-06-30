import os , json , io
from pdf2image import convert_from_path
from google import genai
from google.genai import types
from .models import LabelNutrition  
from .serializers import LabelNutritionSerializers


def get_prompt(nutritional_facts):
    
    return f"""
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
    6. nutritional_facts: All nutrients listed IN ORDER they appear (as ordered array with per100g and per serving values). The figures should exactly match with the json provided {nutritional_facts}
    7. manufacturer_packer_details: Full manufacturer and packer name and address
    8. fssai_details: FSSAI logo present? and FSSAI licence number
    9. importer_country_of_origin: Importer name if any and country of origin
    10. date_of_mfg: Manufacturing and packaging date (may say 'see pack' or similar)
    11. expiry_date: Expiry or best before date (may say 'see pack' or similar)
    13. cost_block: MRP followed by (Inc of all taxes) to the right or below followed by USP also mentioned fully as Unit Sale Price followed by ₹ per gram or liter followed by Batch No followed by packaging date and use by. Also tell whether the order matches the one mentioned respectively.
    14. barcode: Is a barcode present? What type if identifiable?
    15. illustration_disclaimer: Any disclaimer about product illustration or image
    17. jivo_trademark: Any trademark declaration for Jivo brand
    18. compliance_section: Same as the cost_block, this section is ordered as ISO Certification No, followed by EPR Brand Owner Name and lastly the Registration Number
    19. Disclaimer of any signs (* , $ , #) mentioned or not
    
    Return format:
    {{
      "food_name": {{"value": "...", "confidence": "high", "notes": "..."}},
      "product_category": {{"value": "...", "confidence": "high", "notes": "..."}},
      ... (all 19 parameters)
    }}
    """

def run_extraction(pdf_path , item_id):
    
    
    
    nutritional_facts = LabelNutrition.objects.filter(label_item = item_id)
    nutritional_facts_serialized =  LabelNutritionSerializers(nutritional_facts , many=True).data
    print(nutritional_facts)
    PROMPT = get_prompt(nutritional_facts_serialized)
    
    
    # print(PROMPT)
    pages = convert_from_path(
        pdf_path,
        dpi=300,
        poppler_path=r"C:\poppler-26.02.0\Library\bin"  # ← add this line
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
        return data

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw response:\n{raw}")

