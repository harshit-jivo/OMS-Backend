"""Django admin for the label compliance rules.

The rule book is data, not code — the point of moving the 19 hard-coded
parameters out of `service.py` was that the legal desk can change a rule
without a deploy. Until the Compliance Rules screen exists on the web app,
this is where that happens.

`code` is editable on creation and read-only afterwards, mirroring
`serializers.ComplianceRuleSerializer`: issued reports cite it as `rule_id`,
so changing it would orphan them. The two enforce the same rule because a
second way in must not be a way around.
"""
from django.contrib import admin

from .models import ComplianceRule, LabelData


@admin.register(ComplianceRule)
class ComplianceRuleAdmin(admin.ModelAdmin):
    list_display = ('sort_order', 'code', 'name', 'check_type', 'is_active',
                    'is_critical', 'updated_at')
    list_display_links = ('code', 'name')
    # The two fields most often changed in bulk: turning a rule off, and
    # moving it up the checklist.
    list_editable = ('sort_order', 'is_active')
    list_filter = ('check_type', 'is_active', 'is_critical')
    search_fields = ('code', 'name', 'rule_text')
    ordering = ('sort_order', 'code')
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('code', 'name', 'check_type', 'rule_text'),
            'description':
                'Write the rule the way you would explain it to a reviewer. '
                'The sentence is sent to the AI verbatim, so wording is the '
                'whole configuration — be specific about what counts as a '
                'failure, and ask for the label wording to be quoted back.',
        }),
        ('Measurement rules', {
            'fields': ('params',),
            'description':
                'Only for rules whose type is Measurement. Those are computed '
                'from the package dimensions entered on the check form and are '
                'never sent to the AI, so their rule text is documentation '
                'rather than a prompt — and their CODE selects which '
                'measurement runs. Use only codes the backend implements '
                '(PDP_AREA, SMALL_PACKAGE_PDP, VEG_MARK_SIZE, FORT_LOGO_SIZE, '
                'FORT_LOGO_COLOUR); any other code reports itself as '
                'unimplemented rather than passing quietly. Params are '
                'per-check settings, e.g. {"tolerance_de": 10}.',
            'classes': ('collapse',),
        }),
        ('OCR cross-check', {
            'fields': ('critical_tokens', 'is_critical'),
            'description':
                'Optional, and most rules should leave both alone. Tokens are '
                'literal wording that must appear in the text read off the '
                'label, e.g. ["Best Before"] — matching tolerates OCR errors. '
                'A PASS is only ever overturned to FAIL when the rule is '
                'ALSO marked critical, because OCR misses small print and a '
                'false failure costs a reviewer more than an unverified pass. '
                'Both are ignored for a measurement rule: there is no model '
                'verdict to cross-check.',
        }),
        ('Checklist', {'fields': ('is_active', 'sort_order')}),
        ('History', {'fields': ('created_at', 'updated_at'),
                     'classes': ('collapse',)}),
    )

    def get_readonly_fields(self, request, obj=None):
        # Editing an existing rule: `code` is frozen. Creating one: it is not.
        if obj is None:
            return self.readonly_fields
        return (*self.readonly_fields, 'code')


@admin.register(LabelData)
class LabelDataAdmin(admin.ModelAdmin):
    """Past checks, read-only.

    Rows are written by the pipeline and are the record of what was reported
    to whom — editing one would rewrite history rather than correct it. Kept
    visible because "why did it say that?" is answerable only from the stored
    OCR text and report.
    """

    list_display = ('id', 'label_file', 'uploaded_at')
    date_hierarchy = 'uploaded_at'
    readonly_fields = ('label_file', 'ocr_text', 'report_json',
                       'parameter_json', 'uploaded_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
