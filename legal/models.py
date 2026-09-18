from django.db import models

# Create your models here.
class LabelData(models.Model):

    label_file = models.FileField(upload_to = 'labels/')
    parameter_json = models.JSONField(null = True , blank = True)
    uploaded_at = models.DateTimeField(auto_now_add = True)

    # --- Hybrid pipeline (OCR + Gemini) -----------------------------------
    # The deterministic half of the check, kept verbatim. It is what the
    # cross-reference pass reads, and the only way to answer "why did it say
    # that?" about a finding after the fact — the model's own reasoning is
    # gone the moment the response is parsed.
    ocr_text = models.TextField(null=True, blank=True)
    # The rendered report: {"findings": [...], "summary": {...}} as produced by
    # `service.run_label_check`. Separate from `parameter_json` (the previous
    # pipeline's 19-parameter shape) rather than overwriting it, so old rows
    # stay readable and a rollback does not have to reinterpret them.
    report_json = models.JSONField(null=True, blank=True)

    # --- History ----------------------------------------------------------
    # Every check was already stored; what was missing was the ability to open
    # an old one. These three fields are what a history list needs and cannot
    # reconstruct.
    #
    # The rasterised preview, kept because the report is meaningless without
    # the artwork its highlight boxes refer to. The upload itself is usually a
    # PDF, which an <img> cannot render, and re-rendering it on every view
    # would mean running poppler to answer a page load.
    preview_image = models.CharField(max_length=500, blank=True, default='')
    # Who ran it. SET_NULL rather than CASCADE: a compliance record must
    # outlive the account that produced it — deleting a user must not delete
    # the evidence of what was checked and when.
    checked_by = models.ForeignKey(
        'users.User', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='label_checks',
    )
    # Which product's nutrition panel it was compared against, if any.
    label_item = models.ForeignKey(
        'legal.LabelItem', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='label_checks',
    )

    class Meta:
        db_table = 'labels'
        # Newest first: a history list is read from the top, and the only
        # natural order for "what has been checked" is when.
        ordering = ['-uploaded_at']
        
        
        
class LabelItem(models.Model):
    
    item_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add = True)
    
    class Meta:
        db_table = 'label_item'
    

class NutritionUOM(models.Model):
    
    uom_name = models.CharField(max_length = 25)
    uom_unit = models.CharField(max_length = 5)
    
    class Meta:
        db_table = 'nutrition_uom'
        
class LabelNutrition(models.Model):

    label_item =  models.ForeignKey(LabelItem , on_delete = models.CASCADE)
    uom = models.ForeignKey(NutritionUOM , null = True ,on_delete = models.SET_NULL)

    nutrition_name = models.CharField(max_length=125)
    per_serving = models.DecimalField(max_digits = 7 , decimal_places = 3)
    per_100gm = models.DecimalField(max_digits = 7 , decimal_places = 3)

    class Meta:
        db_table = 'label_nutrition'


class ComplianceRule(models.Model):
    """One compliance rule, written in plain English, edited without a deploy.

    The rules used to live inside the prompt string in `service.py` — 19 of
    them, hard-coded, so "FSSAI numbers are now 14 digits" was a code change,
    a review and a deploy. They are rows now: the legal desk writes the rule
    the way they would explain it to a colleague, and the pipeline hands that
    sentence to the model as-is.

    `code` is the stable identity, not `id`
    ---------------------------------------
    The report keys findings by `code` (it is the `rule_id` in the response),
    so it survives a database reload, appears in exported reports, and can be
    quoted in an email. An auto-increment id would silently point at a
    different rule after a restore.
    """

    #: How the rule is judged. AI rules are sent to Gemini as a sentence;
    #: MEASUREMENT rules are not sent at all and are answered by
    #: `legal.dimensions` from the package dimensions the reviewer supplies.
    #:
    #: The split exists because a model cannot measure. Asking Gemini whether
    #: a circle is 4 mm across gets a confident number with nothing behind it,
    #: which is the same reason `schemas.GeminiRuleFinding` refuses to let it
    #: supply coordinates. A measurement rule's `rule_text` is documentation
    #: for the legal desk, not a prompt.
    CHECK_AI = 'AI'
    CHECK_MEASUREMENT = 'MEASUREMENT'
    CHECK_TYPES = [
        (CHECK_AI, 'AI — judged from the label image'),
        (CHECK_MEASUREMENT, 'Measurement — computed from package dimensions'),
    ]

    code = models.CharField(
        max_length=50, unique=True,
        help_text='Stable identifier used as rule_id in the report '
                  '(e.g. FSSAI_LICENCE). Permanent once reports cite it.',
    )
    check_type = models.CharField(
        max_length=20, choices=CHECK_TYPES, default=CHECK_AI,
        help_text='AI rules are judged from the label image. Measurement '
                  'rules are computed from the dimensions entered on the '
                  'check form and are never sent to the AI — their code must '
                  'be one the backend implements.',
    )
    params = models.JSONField(
        default=dict, blank=True,
        help_text='Optional settings for a measurement rule, e.g. '
                  '{"tolerance_de": 10} for the fortification colour check. '
                  'Ignored by AI rules.',
    )
    name = models.CharField(
        max_length=150,
        help_text='Short human name, shown as the checklist heading.',
    )
    rule_text = models.TextField(
        help_text='The rule in plain English, as you would explain it to a '
                  'reviewer. Sent to the model verbatim.',
    )

    #: Literal strings that MUST appear in the OCR text for this rule to be
    #: credibly PASS — the deterministic half of the hybrid check. Optional:
    #: most rules ("ingredients are in descending order by weight") cannot be
    #: reduced to a token, and for those OCR simply has no opinion. See
    #: `service.cross_reference` for exactly how a token is used, and how
    #: conservatively — OCR is noisy, and a false FAIL costs a reviewer more
    #: than an unverified PASS.
    critical_tokens = models.JSONField(
        default=list, blank=True,
        help_text='Optional list of literal strings that must appear in the '
                  'label text, e.g. ["Best Before"]. Case-insensitive. Leave '
                  'empty unless the rule really does hinge on exact wording.',
    )
    is_critical = models.BooleanField(
        default=False,
        help_text='Statutory declarations where a wrong PASS is unacceptable. '
                  'Only these can have a PASS overturned by the OCR '
                  'cross-reference.',
    )

    is_active = models.BooleanField(
        default=True,
        help_text='Inactive rules are not sent to the model and do not appear '
                  'in reports. Deactivate rather than delete, so old reports '
                  'citing the code stay explicable.',
    )
    sort_order = models.PositiveIntegerField(
        default=100,
        help_text='Checklist order. Ties break on code.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'legal_compliance_rule'
        ordering = ['sort_order', 'code']

    def __str__(self):
        return f'{self.code}: {self.name}'