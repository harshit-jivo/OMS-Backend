"""Seed the dimensional rules — the checks computed rather than judged.

These five are not like the twenty-one in `0005`. Their `rule_text` is never
sent anywhere: a MEASUREMENT rule is answered by `legal.dimensions` from the
package dimensions the reviewer enters, and the sentence here exists so the
legal desk can read what the rule is on the Compliance Rules screen. The
`code` is therefore not just an identifier — it SELECTS the implementation
(`dimensions.CHECKS`), which is why these five codes must not be renamed and
why an unknown MEASUREMENT code reports itself as unimplemented rather than
passing quietly.

Seeded ACTIVE, which is safe because of how they degrade
--------------------------------------------------------
A reviewer who uploads a label without touching the dimensions panel gets
these rules SKIPPED, with the reason on the report — not failed. So switching
them on cannot turn a clean report into five failures overnight, and a
reviewer who does fill the panel in gets the checks immediately without
anyone having to know there was a switch to find.

Sort order continues from 0005 (which ends at 210). They sit at the end of
the checklist deliberately: they are the checks that depend on information
from outside the artwork, and grouping them keeps that distinction visible.

`get_or_create` keyed on `code`, as in 0005: a rule the desk has since edited
stays edited if this migration is ever re-run.
"""
from django.db import migrations

SEED = [
    # (code, name, rule_text, params, sort_order)
    (
        'PDP_AREA', 'Principal Display Panel area',
        'The Principal Display Panel is 40% of the largest panel (height x '
        'width) for a rectangular package, 40% of height x average '
        'circumference for a cylindrical, round or oval one, and a share of '
        'total surface area for any other shape — 20% under the FSSAI extract '
        'and 40% under the Legal Metrology extract, so the applicable '
        'regulation must be chosen on the check form. Computed from the '
        'package dimensions, not read off the artwork; every other '
        'dimensional rule is measured against the result.',
        {}, 220,
    ),
    (
        'SMALL_PACKAGE_PDP', 'Small package (10 cm3 or less)',
        'A package with a capacity of 10 cubic centimetres or less may carry '
        'its Principal Display Panel on a card or tape, provided it is firmly '
        'affixed to the package and carries the required information. Only '
        'reported when the declared capacity is 10 cm3 or less.',
        {}, 230,
    ),
    (
        'VEG_MARK_SIZE', 'Veg / non-veg mark — minimum size',
        'The vegetarian or non-vegetarian mark must meet the minimum size for '
        'the PDP area: circle diameter 3/4/6/8 mm, triangle side 2.5/3.5/5/7 '
        'mm, or square side 6/8/12/16 mm for a PDP of up to 100, up to 500, '
        'up to 2500 and above 2500 cm2 respectively. The measured size is '
        'entered on the check form; this rule checks it against the table. '
        'Whether the mark is correct in colour and form is covered separately '
        'by VEG_NONVEG_MARK.',
        {}, 240,
    ),
    (
        'FORT_LOGO_SIZE', 'Fortification logo — dimensions',
        'The fortification logo must be one of the prescribed sizes and must '
        'be scaled proportionately: the outer box is square (A equals B) at '
        'every published size — 20, 40, 80, 160 and 320 mm. The overall A and '
        'B are entered on the check form. The finding reports the full '
        'published artwork set (C, D, a, E, F, G) for that size so the logo '
        'can be checked against it; those sub-elements are not measured.',
        {}, 250,
    ),
    (
        'FORT_LOGO_COLOUR', 'Fortification logo — colour',
        'The fortification logo must be printed in PANTONE 3005 C '
        '(C-100 M-46 Y-2 K-0, #0074C8) and PANTONE BLACK (#231F20). Measured '
        'from the artwork pixels. The blue is the diagnostic half; black is '
        'reported but not judged, because every label carries black text. '
        'Raise "tolerance_de" in params if correct artwork is failing on '
        'colour — the default of 10 already allows for the PDF raster and '
        'JPEG encode the artwork passes through.',
        {'tolerance_de': 10}, 260,
    ),
]


def seed(apps, schema_editor):
    ComplianceRule = apps.get_model('legal', 'ComplianceRule')
    for code, name, rule_text, params, sort_order in SEED:
        ComplianceRule.objects.get_or_create(
            code=code,
            defaults={
                'name': name,
                'rule_text': rule_text,
                'check_type': 'MEASUREMENT',
                'params': params,
                # No tokens and not critical: `cross_reference` compares OCR
                # text against the model's verdict, and neither half of that
                # applies to a rule the model never saw.
                'critical_tokens': [],
                'is_critical': False,
                'is_active': True,
                'sort_order': sort_order,
            },
        )


def unseed(apps, schema_editor):
    """Remove only the rows still untouched since seeding.

    A reverse that deleted edited rules would throw away the legal desk's
    work to undo a schema change they had nothing to do with.
    """
    ComplianceRule = apps.get_model('legal', 'ComplianceRule')
    for code, name, rule_text, _params, _sort in SEED:
        ComplianceRule.objects.filter(
            code=code, name=name, rule_text=rule_text).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('legal', '0008_compliance_rule_check_type'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
