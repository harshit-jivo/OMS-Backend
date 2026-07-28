# OMS-Backend `legal` Module - Beginner-Friendly Deep Notes

This module handles food-label PDF upload and AI extraction for legal/FSSAI review support.

- `legal/models.py` stores uploaded files and extracted JSON.
- `legal/service.py` converts PDF first page to image and sends it to Gemini.
- `legal/views.py` handles upload and persistence flow.
- `legal/serializers.py` validates input shape.
- `legal/urls.py` exposes one endpoint:
  - `POST /api/legal/upload/`

Mounted in [OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py):
- `path('api/legal/' , include('legal.urls'))`

## 1) Files in this module

- [legal/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\models.py)
- [legal/serializers.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\serializers.py)
- [legal/service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\service.py)
- [legal/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\views.py)
- [legal/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\urls.py)
- Migrations:
  - [legal/migrations/0001_initial.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\migrations\0001_initial.py)
  - [legal/migrations/0002_remove_labeldata_parmeter_json_and_more.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\migrations\0002_remove_labeldata_parmeter_json_and_more.py)

## 2) Why this module exists

OMS uses this as a helper for legal/compliance preparation:
- Upload a label PDF from frontend.
- Persist the original file.
- Extract structured fields from the label image using AI.
- Return the structured extraction result and keep it for later review.

This is **not** currently a complete legal-validator pipeline; it is an extraction assist.

## 3) Data model and table

File: [legal/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\models.py)

- Model: `LabelData`
- DB table: `labels` (`Meta.db_table`)
- Columns:
  - `id` (auto PK)
  - `label_file` (`FileField(upload_to='labels/')`)
  - `parameter_json` (`JSONField`, nullable/blank)
  - `uploaded_at` (`DateTimeField(auto_now_add=True)`)

Migration history:
- `0001_initial`: created `parmeter_json` (typo) column.
- `0002...`: removed typo field and added correct `parameter_json`.

## 4) Route and request/response

File: [legal/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\urls.py)

- `POST /api/legal/upload/` -> [FeedtoAIView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\views.py)

## 5) Endpoint flow (`/api/legal/upload/`)

Handler class: `FeedtoAIView` in [legal/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\views.py)

Flow details:
1. Reads `label_file` from `request.data`.
2. If no file: returns `400` with error.
3. Validates via serializer [LabelUploadSerializer](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\serializers.py) (it only accepts `label_file`).
4. Saves row using ORM:
   - `LabelData(label_file=<uploaded file>)`
5. Gets file path on disk:
   - `instance.label_file.path`
6. Calls `run_extraction(label_path)` from [legal/service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\service.py).
7. Writes returned extraction JSON into `instance.parameter_json`.
8. `instance.save()` then returns:
   - `file`: saved file name
   - `parameters`: extracted json
   - HTTP `201`

Success dependency:
- This endpoint’s business logic is synchronous: upload waits for PDF render + external LLM response before returning.

## 6) AI extraction service internals

File: [legal/service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\service.py)

- Imports:
  - `convert_from_path` from `pdf2image`
  - `google.genai` client
- Steps in `run_extraction(pdf_path)`:
  - Convert PDF to images at 300 DPI.
  - Use `pages[0]` (first page only).
  - Convert first page to JPEG bytes.
  - Call Gemini model:
    - `gemini-3.1-flash-lite`
    - prompt asks for structured FSSAI-oriented fields (`food_name`, `product_category`, `nutritional_facts`, `manufacturer...`, etc.)
  - Strip fenced-code markers if returned (` ```json ... ``` `).
  - `json.loads` the result and return dict.
  - On parse failure: logs raw response and returns `None` (implicit).

Important: prompt currently asks for multiple numbered fields, but one numbering mismatch is present in prompt text (`13`, `14`, `15`, `17`, `18`, `19`) with gaps.

## 7) ORM equivalent SQL mental model

- Save upload:
  - `INSERT INTO labels (label_file, parameter_json, uploaded_at) VALUES (...)`
- Read one row by id:
  - `SELECT * FROM labels WHERE id = ...`

There are no list/read API endpoints in this module by default, only upload action with creation flow.

## 8) What to check as MERN dev bridge

Think of this as:
- a single POST upload endpoint in Express + Mongoose;
- model stores file path plus extracted JSON blob;
- request waits inline for OCR/LLM call and returns both stored file and extracted payload;
- no worker queue/background job around the extraction step (so high latency possible).

## 9) Operational risks / onboarding notes

1. Parser configuration typo:
   - view uses `parser_class` instead of DRF expected `parser_classes`.
   - In many DRF setups multipart uploads may fail to parse as intended.
2. Security risk:
   - API key is hardcoded directly in code in [legal/service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\legal\service.py).
3. No authentication/permission guards in this module (`permission_classes` not set).
4. PDF dependency:
   - `poppler_path` is hardcoded to `C:\poppler-26.02.0\Library\bin`, so container/env mismatch will break extraction.
5. Failure path quality:
   - if Gemini returns non-JSON, extraction returns `None` and still gets saved as null.

## 10) Related files and next debugging checkpoints

- Verify env-safe API key handling (should move to settings/env variable).
- Verify upload endpoint under auth flow if used in production.
- Validate extracted JSON schema against front-end expectations.
- Add cleanup policy for old uploaded PDFs if retention control is needed.
- Add endpoint to fetch/list extraction results by id/date if auditors need traceability.

