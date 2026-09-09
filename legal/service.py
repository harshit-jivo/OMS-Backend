"""The hybrid label-check pipeline: Tesseract first, Gemini second, then us.

    upload -> render to an image -> OCR (deterministic)
                                 -> Gemini (image + OCR text + rules)
                                 -> cross-reference -> report

What each stage is for
----------------------
* **OCR** does not understand the label, but everything it returns was really
  printed there. It is the ground truth the third stage checks against.
* **Gemini** judges rules written in English against a picture — the thing no
  amount of regex does. It is also the stage that can be confidently wrong,
  which is why it is not the last one.
* **Cross-reference** is ours. It compares the model's verdicts against the
  OCR text for rules the legal desk marked critical, and either corroborates
  a finding or overturns it. See `cross_reference`.

What this replaces
------------------
A single call that sent a 100-line hard-coded prompt describing 19 fixed
parameters, stripped ``` fences off the reply by hand, and returned `None`
when `json.loads` raised — which reached the browser as an empty report with
no error. Three things changed:

1. The rules are rows (`models.ComplianceRule`), written in English by the
   people who own them, so a rule change is an edit and not a deploy.
2. The response is schema-constrained (`schemas.GeminiLabelReport` handed to
   the SDK as `response_schema`), so there are no fences to strip and a
   malformed reply is an exception with a message rather than a `None`.
3. The API key comes from settings. It was committed in the source of this
   file — see `_client`.
"""
import io
import json
import logging
import os

from django.conf import settings

from . import ocr
from .models import ComplianceRule, LabelNutrition
from .schemas import GeminiLabelReport, Region, RuleFinding

logger = logging.getLogger(__name__)

#: Extensions we open directly with Pillow. Anything else is treated as a PDF
#: and rasterised — the legal desk uploads both, and which one arrives is not
#: worth making them declare.
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}


#: Resolution a PDF label is rasterised at, for OCR and for the model.
#:
#: 300 was the original and it is not enough. A retail label is physically
#: small — the Jivo mustard oil pack is 2.6 x 8 cm — so 300 dpi renders the
#: whole artwork to about 767x1193, and the nutrition panel's row labels come
#: out too small for Tesseract to read. Measured on that label:
#:
#:     dpi  words  Energy  Total Fat  Saturated Fat   time
#:     300    300    no       no          no          2.6s
#:     500    379    yes      yes         yes         2.8s
#:     700    386    yes      yes         yes         3.2s
#:
#: 500 reads the table; 700 finds almost nothing more and costs more on every
#: check. The consequence of 300 was not merely a missing highlight: with
#: "Energy" unreadable, the only anchor left for a nutrition finding was the
#: word "kcal" in the FOOTNOTE, which put a box on the wrong part of the page.
#:
#: The preview is thumbnailed from this (`PREVIEW_MAX_EDGE`), so a sharper
#: render costs the browser nothing.
RENDER_DPI = 500


class LabelCheckError(RuntimeError):
    """The check could not be completed. The message is shown to the user."""


# ---------------------------------------------------------------------------
# Stage 0 — get a picture
# ---------------------------------------------------------------------------

def load_image(path: str):
    """The label as a single PIL image: page 1 of a PDF, or the image itself.

    Only the first page. A label artwork PDF is one page; a multi-page upload
    is a mistake worth surfacing as "we checked page 1" rather than silently
    charging for five model calls.
    """
    from PIL import Image  # local: Pillow is heavy and only needed here

    suffix = os.path.splitext(path)[1].lower()

    if suffix in IMAGE_SUFFIXES:
        image = Image.open(path)
        # Normalise to RGB: PNG screenshots arrive as RGBA and JPEG encoding
        # of an alpha channel raises rather than flattening.
        return image.convert('RGB')

    from pdf2image import convert_from_path

    # `poppler_path` empty means "on PATH" — correct on the Linux host, and
    # configurable because Windows dev boxes install poppler wherever.
    poppler = getattr(settings, 'POPPLER_PATH', '') or None
    try:
        pages = convert_from_path(path, dpi=RENDER_DPI, poppler_path=poppler)
    except Exception as exc:  # noqa: BLE001 — pdf2image wraps several
        raise LabelCheckError(
            'Could not read that file as a PDF or an image. Upload the label '
            'artwork as a PDF, PNG or JPEG.'
        ) from exc

    if not pages:
        raise LabelCheckError('That PDF has no pages.')
    return pages[0]


def _image_bytes(image) -> bytes:
    """JPEG bytes for the model call."""
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=95)
    return buffer.getvalue()


#: Longest edge of the preview saved for the browser. The page shows the label
#: beside the report in a column a few hundred pixels wide, so shipping the
#: 300-dpi render (an A4 page is ~2500x3500, several MB of PNG) would cost the
#: reviewer seconds of loading for detail their screen cannot show. They open
#: the original file when they need to zoom.
PREVIEW_MAX_EDGE = 1400


def save_preview(image, source_name: str) -> str:
    """Save a browser-displayable copy of the label and return its URL.

    The upload itself is often a PDF, which an `<img>` cannot render — so the
    page could show nothing at all for the most common input. Rasterising
    already happened (`load_image`), so this just keeps the result instead of
    discarding it.

    Written through `default_storage` rather than to a path built by hand, so
    it keeps working if media ever moves off local disk.
    """
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    from PIL import Image

    preview = image.copy()
    preview.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.LANCZOS)

    buffer = io.BytesIO()
    preview.save(buffer, format='PNG', optimize=True)

    stem = os.path.splitext(os.path.basename(source_name))[0]
    # `default_storage.save` de-duplicates the name itself, so two labels with
    # the same filename do not overwrite one another's preview.
    saved = default_storage.save(f'labels/previews/{stem}.png',
                                 ContentFile(buffer.getvalue()))
    return default_storage.url(saved)


# ---------------------------------------------------------------------------
# Stage 1 — the rules and the reference data
# ---------------------------------------------------------------------------

def active_rules():
    """Every rule to check, in checklist order."""
    return list(ComplianceRule.objects.filter(is_active=True))


def nutrition_reference(item_id) -> list[dict]:
    """The nutrition panel we hold for this item, for the model to compare to.

    Empty when no item was chosen — the check still runs, it just cannot say
    anything about whether the printed panel matches our master data.
    """
    if not item_id:
        return []
    rows = LabelNutrition.objects.filter(label_item=item_id).values(
        'nutrition_name', 'per_100gm', 'per_serving')
    return [
        {
            'nutrition_name': row['nutrition_name'],
            # Decimal is not JSON-serialisable and the prompt is JSON.
            'per_100gm': float(row['per_100gm']),
            'per_serving': float(row['per_serving']),
        }
        for row in rows
    ]


def build_prompt(rules, ocr_text: str, nutrition: list[dict]) -> str:
    """The instruction sent alongside the image.

    Three things go in, and the order matters: who the model is, what it may
    rely on (the OCR text — offered as an aid, explicitly NOT as a substitute
    for reading the picture), and the rules themselves, verbatim as the legal
    desk wrote them.

    The output shape is NOT described here. It is enforced by
    `response_schema` on the call, which is stricter than any wording and
    cannot drift from `schemas.py`.
    """
    rule_block = '\n\n'.join(
        f'- rule_id: {rule.code}\n'
        f'  rule_name: {rule.name}\n'
        f'  rule: {rule.rule_text}'
        for rule in rules
    )

    nutrition_block = (
        json.dumps(nutrition, indent=2) if nutrition
        else 'No reference nutrition data was supplied for this product. Do '
             'not comment on whether printed values match our records.'
    )

    ocr_block = ocr_text.strip() or (
        'OCR was unavailable for this label. Rely on the image alone, and say '
        'so in your remarks where the reading is uncertain.'
    )

    # Deterministic facts, extracted by us rather than read by the model.
    # A 14-digit licence number is the one piece of label data where a
    # transcription slip is a statutory problem and a regex is strictly better
    # than a vision model — OCR reads the digits reliably even where it
    # mangles the surrounding words (observed: "FSSAI" read as "FSSA", the
    # number beside it read perfectly). Offering the count spares the model
    # from counting digits, which is exactly the kind of task it fails
    # confidently.
    licences = ocr.fssai_numbers(ocr_text)
    facts_block = (
        '\n'.join(
            f'- A 14-digit sequence was read from the label: {number} '
            f'({len(number)} digits).'
            for number in licences
        )
        if licences
        else '- No 14-digit sequence was found in the label text.'
    )

    return f"""You are an FSSAI label-compliance reviewer for Indian packaged food.

You are given a product label image, the raw text an OCR engine read from it,
and a list of compliance rules. Judge EVERY rule against the label.

The OCR text is an aid, not the truth. It drops characters, joins words and
misreads small print. Where OCR and the image disagree, trust the image. Never
report a declaration as absent solely because OCR did not pick it up.

For every rule return: the rule_id and rule_name exactly as given, a status of
PASS or FAIL, remarks, and evidence_text.

evidence_text is the exact wording you read on the label that this finding
rests on — copied character for character, so it can be found on the page and
highlighted for the reviewer. Copy only the words that are actually printed,
not your description of them. Leave it EMPTY when the rule fails because the
declaration is absent: there is nothing to point at, and inventing wording
would highlight the wrong part of the label. Remarks are for a compliance reviewer who has not
seen the label: say what you found, quote the label wording you relied on, and
where it appears. If a rule cannot be judged because the relevant area is
unreadable, answer FAIL and say exactly that. Never answer with just "OK".

Return one finding per rule, in the order the rules are listed. Do not invent
rules, do not merge them, and do not skip any.

--- OCR TEXT FROM THE LABEL ---
{ocr_block}

--- VERIFIED BY EXACT EXTRACTION (trust these over your own reading) ---
{facts_block}

--- OUR REFERENCE NUTRITION DATA FOR THIS PRODUCT ---
{nutrition_block}

--- RULES TO CHECK ---
{rule_block}
"""


# ---------------------------------------------------------------------------
# Stage 2 — the model
# ---------------------------------------------------------------------------

def _client():
    """The configured Gemini client.

    SECURITY: the previous version of this file carried a live API key as a
    string literal, committed to the repository. It is read from settings now
    (`GEMINI_API_KEY`, sourced from `.env`). The old key must be treated as
    disclosed and rotated — anything with repository access has had it.
    """
    from google import genai  # local: SDK import is slow and optional at boot

    api_key = getattr(settings, 'GEMINI_API_KEY', '') or ''
    if not api_key:
        raise LabelCheckError(
            'The label checker is not configured: GEMINI_API_KEY is unset. '
            'Add it to the backend .env file.'
        )
    return genai.Client(api_key=api_key)


def call_gemini(image_bytes: bytes, prompt: str) -> GeminiLabelReport:
    """Ask the model, with the response schema enforced by the SDK.

    Returns a validated `GeminiLabelReport`. Raises `LabelCheckError` with a
    message worth showing a user — a stack trace about a 429 is not.
    """
    from google.genai import types

    client = _client()
    model = getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash')

    try:
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type='application/json',
                response_schema=GeminiLabelReport,
                # Compliance review is not a creative task: the same label
                # checked twice should read the same way.
                temperature=0.0,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — SDK raises transport + API types
        logger.exception('Gemini call failed for label check')
        raise LabelCheckError(f'The AI review could not be completed: {exc}') from exc

    # `parsed` is the SDK's already-validated object when a response_schema was
    # supplied. It is None when the model returned nothing usable (a safety
    # block, or an empty candidate), which the old code could not distinguish
    # from a parse failure.
    parsed = getattr(response, 'parsed', None)
    if parsed is not None:
        return parsed if isinstance(parsed, GeminiLabelReport) else \
            GeminiLabelReport.model_validate(parsed)

    raw = (getattr(response, 'text', '') or '').strip()
    if not raw:
        raise LabelCheckError(
            'The AI returned an empty response for this label. This usually '
            'means the image was rejected — try a clearer scan.'
        )
    try:
        return GeminiLabelReport.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError et al
        logger.error('Unparseable Gemini response: %s', raw[:2000])
        raise LabelCheckError(
            'The AI response did not match the expected format.'
        ) from exc


# ---------------------------------------------------------------------------
# Stage 3 — cross-reference (ours)
# ---------------------------------------------------------------------------

def cross_reference(findings, rules, ocr_text: str) -> list[RuleFinding]:
    """Check the model's verdicts against the OCR text.

    This is the "hybrid" in hybrid pipeline, and it is deliberately
    conservative in one direction and not the other:

    * A rule whose `critical_tokens` ARE in the OCR text gets
      `ocr_verified=True`. If the model failed it anyway, the remark says both
      things — the model may well be right (the text is present but wrong, or
      in the wrong place), and a reviewer needs to know the disagreement
      exists rather than have it resolved for them.

    * A rule whose tokens are ABSENT sets `ocr_verified=False`. That only
      overturns a PASS when the rule is `is_critical`, because OCR misses text
      constantly — folds, low contrast, rotated print. Overturning every
      unverified PASS would bury a reviewer in false failures and they would
      stop reading the report, which is worse than the thing we are guarding
      against.

    * A rule with no tokens leaves `ocr_verified` as None. Most rules are
      judgements ("ingredients in descending order by weight") that no token
      can corroborate; claiming OCR verified them would be a lie of omission.

    When OCR itself was unavailable, `ocr_text` is empty and every finding
    keeps `ocr_verified=None` — the check ran on the model alone and the
    report says so.
    """
    by_code = {rule.code: rule for rule in rules}
    verified: list[RuleFinding] = []

    for finding in findings:
        rule = by_code.get(finding.rule_id)
        tokens = list(getattr(rule, 'critical_tokens', None) or []) if rule else []

        result = RuleFinding(
            rule_id=finding.rule_id,
            rule_name=finding.rule_name,
            status=finding.status,
            remarks=finding.remarks,
            evidence_text=getattr(finding, 'evidence_text', '') or '',
        )

        if not ocr_text.strip() or not tokens:
            verified.append(result)
            continue

        missing = [token for token in tokens if not ocr.contains(ocr_text, token)]
        result.ocr_verified = not missing

        if not missing:
            if result.status == 'FAIL':
                result.remarks += (
                    ' [OCR cross-check: the expected wording '
                    f'({", ".join(tokens)}) WAS found in the label text, so '
                    'review this failure by eye before acting on it.]'
                )
        else:
            absent = ', '.join(missing)
            if result.status == 'PASS' and rule is not None and rule.is_critical:
                result.status = 'FAIL'
                result.remarks += (
                    f' [OCR cross-check: overturned to FAIL — the AI passed '
                    f'this rule but the required wording ({absent}) does not '
                    'appear anywhere in the text read from the label. This is '
                    'a critical declaration, so it must be confirmed by eye.]'
                )
            else:
                result.remarks += (
                    f' [OCR cross-check: could not find ({absent}) in the '
                    'label text. OCR misses small or low-contrast print, so '
                    'this is a prompt to verify, not a failure in itself.]'
                )

        verified.append(result)

    return verified


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def attach_regions(findings, read, rules) -> None:
    """Point each finding at the place on the label it is about.

    Mutates `findings` in place — it is the same list the caller is about to
    serialise, and returning a copy would invite one of the two to be used by
    mistake.

    Deliberately separate from `cross_reference`: that decides WHAT the verdict
    is, this decides WHERE to point. Keeping them apart means a change to the
    highlighting cannot alter a verdict, which is the property that matters
    when someone later wants the boxes to behave differently.

    Two things are tried, in order:

    1. `evidence_text` — the wording the model says it read. This is the good
       case and covers every PASS.
    2. The rule's `critical_tokens` — for a finding whose quotation could not
       be located, the declaration's own wording is still worth pointing at.

    A FAIL for an ABSENT declaration finds nothing, and that is correct: there
    is no place on the label to point at something that is not printed. The UI
    says so rather than drawing a box somewhere plausible.
    """
    by_code = {rule.code: rule for rule in rules}

    for finding in findings:
        if not read or not read.words:
            continue

        rule = by_code.get(finding.rule_id)
        candidates = [finding.evidence_text]
        candidates.extend(getattr(rule, 'critical_tokens', None) or [])

        for phrase in candidates:
            if not (phrase or '').strip():
                continue
            boxes = ocr.locate(phrase, read.words, read.size)
            if boxes:
                finding.regions = [Region(**box) for box in boxes]
                break


def summarise(findings) -> dict:
    """Counts the frontend would otherwise recompute on every render."""
    total = len(findings)
    failed = sum(1 for f in findings if f.status == 'FAIL')
    return {
        'total': total,
        'passed': total - failed,
        'failed': failed,
        # A label is compliant only if every rule passed. Stated as a field
        # rather than left for each client to derive, so the web page, an
        # export and any future mobile view cannot disagree about it.
        'compliant': total > 0 and failed == 0,
    }


def run_label_check(file_path: str, item_id=None) -> dict:
    """Check one uploaded label against every active rule.

    Returns the report dict the API serves:

        {"findings": [...], "summary": {...}, "ocr_text": "...",
         "ocr_available": bool, "rule_count": int, "preview_url": "..."}

    Raises `LabelCheckError` for anything the user should be told about.
    """
    rules = active_rules()
    if not rules:
        raise LabelCheckError(
            'No compliance rules are configured yet. Add rules on the '
            'Compliance Rules screen before checking a label.'
        )

    image = load_image(file_path)

    # Best-effort: a preview that could not be written is a page without a
    # picture, which is a worse report — not a failed check. The findings are
    # what the reviewer came for.
    try:
        preview_url = save_preview(image, file_path)
    except Exception:  # noqa: BLE001 — storage backends raise their own types
        logger.exception('Could not save label preview for %s', file_path)
        preview_url = ''

    # OCR is optional infrastructure: losing it costs the cross-reference, not
    # the check. The report carries `ocr_available` so the UI can say which
    # kind of answer this is instead of quietly giving a weaker one.
    try:
        read = ocr.read(image)
        ocr_text = read.text
        ocr_available = True
    except ocr.OcrUnavailable as exc:
        logger.warning('OCR unavailable, continuing without it: %s', exc)
        read = None
        ocr_text = ''
        ocr_available = False

    prompt = build_prompt(rules, ocr_text, nutrition_reference(item_id))
    report = call_gemini(_image_bytes(image), prompt)
    findings = cross_reference(report.findings, rules, ocr_text)
    # Highlighting is best-effort decoration on a report that is already
    # complete: a locator failure must not lose the findings.
    try:
        attach_regions(findings, read, rules)
    except Exception:  # noqa: BLE001
        logger.exception('Could not locate highlight regions for %s', file_path)

    return {
        'findings': [f.model_dump() for f in findings],
        'summary': summarise(findings),
        'ocr_text': ocr_text,
        'ocr_available': ocr_available,
        'rule_count': len(rules),
        'preview_url': preview_url,
    }
