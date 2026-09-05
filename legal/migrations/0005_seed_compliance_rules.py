"""Seed the rule book with the checks that used to be hard-coded.

The pipeline this replaces carried 19 parameters inside a prompt string in
`legal/service.py`. Deleting that file without moving the knowledge into the
new table would have quietly dropped every check the legal desk relied on, so
each one is reproduced here as a `ComplianceRule` row, rewritten from an
extraction instruction ("extract the FSSAI licence number") into the rule it
was really testing ("an FSSAI licence number is present and has 14 digits").

These are a STARTING POINT, not a fixture. The whole reason rules are rows is
that the legal desk owns them: editing, deactivating or adding to them is
expected and needs no migration. Re-running this migration will not restore or
overwrite an edited rule — `get_or_create` keyed on `code` means a rule the
desk has changed stays changed, which is the behaviour that makes the seed
safe to keep in the migration history.

`critical_tokens` is set on only a handful. A token is only worth declaring
where the statute really does mandate an exact phrase — those are the rules
where OCR can corroborate the model. Everything else is a judgement, and
`service.cross_reference` leaves it to the model alone rather than pretending
to verify it.
"""
from django.db import migrations

SEED = [
    # (code, name, rule_text, critical_tokens, is_critical, sort_order)
    (
        'FOOD_NAME', 'Name of the food',
        'The label must declare the name of the food product clearly and '
        'legibly on the principal display panel. A brand name alone is not a '
        'food name.',
        [], True, 10,
    ),
    (
        'VEG_NONVEG_MARK', 'Veg / non-veg symbol',
        'The label must carry the correct symbol: a green filled circle inside '
        'a green square outline for vegetarian food, or a brown/red filled '
        'triangle inside a brown square outline for non-vegetarian food. State '
        'which symbol and colour you can see.',
        [], True, 20,
    ),
    (
        'INGREDIENTS_LIST', 'Ingredients list',
        'An ingredients list must be present, introduced by the word '
        '"Ingredients", listing every ingredient in descending order of its '
        'weight at the time of manufacture. Compound ingredients must show '
        'their own constituents in brackets.',
        ['Ingredients'], False, 30,
    ),
    (
        'ALLERGEN_DECLARATION', 'Allergen declaration',
        'If the product contains or may contain any major allergen (milk, '
        'eggs, fish, crustaceans, tree nuts, peanuts, wheat/gluten, soy, or '
        'added sulphites), it must be declared. FAIL only if the ingredients '
        'plainly indicate an allergen that is not declared anywhere.',
        [], False, 40,
    ),
    (
        'NUTRITION_PANEL', 'Nutritional information panel',
        'A nutritional information panel must be present, giving values per '
        '100 g or per 100 ml, and per serving where a serving size is '
        'declared. Where reference nutrition data is supplied to you, compare '
        'each printed value against it and fail on any mismatch, naming the '
        'nutrient and both values.',
        [], True, 50,
    ),
    (
        'SERVING_SIZE', 'Serving size declaration',
        'Where the nutrition panel gives per-serving values, the serving size '
        'and the number of servings per package must be declared.',
        [], False, 60,
    ),
    (
        'FSSAI_LICENCE', 'FSSAI logo and licence number',
        'The FSSAI logo must appear together with the licence number directly '
        'beneath it. The licence number must be exactly 14 digits. Quote the '
        'number you read and count its digits.',
        ['FSSAI'], True, 70,
    ),
    (
        'MANUFACTURER_DETAILS', 'Manufacturer / packer name and address',
        'The complete name and address of the manufacturer, packer or '
        'marketer must be declared, including a full postal address. "Marketed '
        'by" and "Manufactured by" must each name a full address where both '
        'appear.',
        [], True, 80,
    ),
    (
        'COUNTRY_OF_ORIGIN', 'Importer and country of origin',
        'For imported products the country of origin and the importer name and '
        'address must be declared. For a product manufactured in India this '
        'rule PASSES if the manufacturer address establishes Indian origin.',
        [], False, 90,
    ),
    (
        'MFG_DATE', 'Date of manufacture / packing',
        'The date of manufacture or packing must be declared. It may legitimately '
        'read "refer to body of the pack" if the date is printed elsewhere on '
        'the package.',
        [], True, 100,
    ),
    (
        'EXPIRY_DATE', 'Best before / use by date',
        'A best-before or use-by date must be declared. It may legitimately '
        'read "refer to body of the pack".',
        ['Best Before'], True, 110,
    ),
    (
        'MRP_DECLARATION', 'MRP inclusive of all taxes',
        'The maximum retail price must be declared and must be immediately '
        'followed by the words "(Incl. of all taxes)" or equivalent wording.',
        ['MRP'], True, 120,
    ),
    (
        'UNIT_SALE_PRICE', 'Unit sale price',
        'The unit sale price must be declared in rupees per gram, per '
        'kilogram, per millilitre or per litre as appropriate to the product.',
        [], False, 130,
    ),
    (
        'NET_QUANTITY', 'Net quantity declaration',
        'The net quantity must be declared in metric units (g, kg, ml, l) on '
        'the principal display panel.',
        [], True, 140,
    ),
    (
        'BATCH_NUMBER', 'Batch / lot number',
        'A batch number, lot number or code number must be declared, '
        'introduced by "Batch No", "Lot No" or "Code No".',
        [], False, 150,
    ),
    (
        'COST_BLOCK_ORDER', 'Order of the pricing block',
        'Within the pricing block the declarations must appear in this order: '
        'MRP (Incl. of all taxes), then unit sale price, then batch number, '
        'then packing date, then use-by date. Fail if the printed order '
        'differs, and say what order you actually see.',
        [], False, 160,
    ),
    (
        'CUSTOMER_CARE', 'Customer care contact',
        'A customer care contact must be declared — an email address, a '
        'telephone number, or both.',
        [], False, 170,
    ),
    (
        'BARCODE_PRESENT', 'Barcode',
        'A scannable barcode must be present on the package.',
        [], False, 180,
    ),
    (
        'EPR_COMPLIANCE', 'EPR and certification block',
        'The compliance block must give the ISO certification number, then the '
        'EPR brand owner name, then the EPR registration number, in that '
        'order. Fail if any is missing or the order differs.',
        [], False, 190,
    ),
    (
        'FOOTNOTE_SYMBOLS', 'Footnote symbols are explained',
        'Every reference symbol used in the nutrition table or elsewhere '
        '(*, #, $, ^) must have a matching footnote explaining it. Fail if a '
        'symbol is used with no corresponding explanation, naming the symbol.',
        [], False, 200,
    ),
    (
        'ILLUSTRATION_DISCLAIMER', 'Serving suggestion disclaimer',
        'Where the pack shows a picture of prepared food or of items not '
        'included, a disclaimer such as "serving suggestion" or "illustration '
        'purpose only" must appear.',
        [], False, 210,
    ),
]


def seed_rules(apps, schema_editor):
    ComplianceRule = apps.get_model('legal', 'ComplianceRule')
    for code, name, rule_text, tokens, critical, order in SEED:
        # get_or_create, not update_or_create: a rule the legal desk has since
        # reworded is theirs, and a re-run must not quietly revert it.
        ComplianceRule.objects.get_or_create(
            code=code,
            defaults={
                'name': name,
                'rule_text': rule_text,
                'critical_tokens': tokens,
                'is_critical': critical,
                'is_active': True,
                'sort_order': order,
            },
        )


def unseed_rules(apps, schema_editor):
    """Remove only the rules this migration created, by code.

    Anything the desk added afterwards is not ours to delete on a rollback.
    """
    ComplianceRule = apps.get_model('legal', 'ComplianceRule')
    ComplianceRule.objects.filter(code__in=[row[0] for row in SEED]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('legal', '0004_label_compliance_rules'),
    ]

    operations = [
        migrations.RunPython(seed_rules, unseed_rules),
    ]
