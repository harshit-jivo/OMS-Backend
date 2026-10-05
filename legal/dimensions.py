"""Stage 2b — the dimensional checks, as findings.

`metrology` holds the knowledge (the statutory tables and the arithmetic).
This module is the pipeline stage: it takes the rules the legal desk marked
`MEASUREMENT`, runs the matching check, and returns `RuleFinding` objects in
exactly the shape `cross_reference` produces for the AI rules.

That sameness is the design, not a coincidence. Because a measurement finding
is a `RuleFinding` like any other, the checklist, the Markdown export, the
history detail endpoint and `summarise()` all handle it with no changes at
all — the report stays ONE document instead of growing a second half that
every consumer has to learn about.

Applicability, without inventing a third status
-----------------------------------------------
`schemas` allows PASS and FAIL and nothing else, deliberately. But "this label
is not fortified" is not a pass and not a failure, and emitting three FAILs
for every unfortified label would train reviewers to ignore failures.

So an inapplicable rule is not emitted at all — the same way an inactive rule
never appears. What it is NOT is silent: every skip is returned alongside the
findings with its reason, and `service` puts them in the report. A rule that
vanished without explanation would be the worst of the three options.

Why a check can FAIL for a reason that is not the label's fault
---------------------------------------------------------------
"The veg mark size was not supplied" is a FAIL here, not a skip. The
difference from the case above is that the reviewer asked for the check — they
ticked the mark as present — and then did not give it what it needs. An
unanswerable question that was asked has to be visible; only a question that
was never asked is skipped.
"""
from __future__ import annotations

import logging

from . import metrology
from .schemas import RuleFinding

logger = logging.getLogger(__name__)

#: Rule codes this module knows how to check. A `MEASUREMENT` rule whose code
#: is not here is reported as unimplemented rather than dropped — see
#: `run_dimension_checks`.
PDP_AREA = 'PDP_AREA'
SMALL_PACKAGE_PDP = 'SMALL_PACKAGE_PDP'
VEG_MARK_SIZE = 'VEG_MARK_SIZE'
FORT_LOGO_SIZE = 'FORT_LOGO_SIZE'
FORT_LOGO_COLOUR = 'FORT_LOGO_COLOUR'

SUPPORTED = (PDP_AREA, SMALL_PACKAGE_PDP, VEG_MARK_SIZE, FORT_LOGO_SIZE,
             FORT_LOGO_COLOUR)


def _applicable(code: str, spec) -> str:
    """'' when the rule applies to this pack, else why it does not.

    The reason is a sentence, because it is shown to the reviewer verbatim.
    """
    if code == SMALL_PACKAGE_PDP and not metrology.is_small_package(spec):
        return ('the package is over 10 cubic centimetres, so the card-or-'
                'tape exemption does not apply')
    if code == VEG_MARK_SIZE and spec.veg_mark == 'NONE':
        return 'no vegetarian or non-vegetarian mark was declared for this pack'
    if code in (FORT_LOGO_SIZE, FORT_LOGO_COLOUR) and not spec.fortified:
        return 'this product was not declared as fortified'
    return ''


# ---------------------------------------------------------------------------
# The individual checks
#
# Each returns (status, remarks). They never raise: a check that cannot be
# performed says so as a FAIL with the reason, because a reviewer can act on
# a sentence and cannot act on a stack trace.
# ---------------------------------------------------------------------------

def _describe_basis(spec, area_cm2: float) -> str:
    """How the PDP was arrived at, in the reviewer's own terms."""
    if spec.shape == 'RECTANGULAR':
        basis = (f'40% of the largest panel, {spec.panel_width_mm} x '
                 f'{spec.panel_height_mm} mm')
    elif spec.shape == 'CYLINDRICAL':
        basis = (f'40% of height {spec.height_mm} mm x average circumference '
                 f'{spec.circumference_mm} mm')
    else:
        fraction = metrology.PDP_FRACTION[f'OTHER_{spec.regulation}']
        regulation = spec.regulation.replace('_', ' ').title()
        basis = (f'{fraction:.0%} of a total surface area of '
                 f'{spec.surface_area_cm2} cm2, per the {regulation} extract')
    return f'PDP = {area_cm2:.1f} cm2 ({basis}).'


def check_pdp_area(spec, image, params) -> tuple:
    """The basis every other dimensional check is measured against.

    Not a verdict on the label — there is nothing here the artwork can get
    wrong — so the remark opens by saying so. It is emitted as a finding
    anyway because it is the audit trail: without it, "why did it demand
    4 mm?" has no answer on the report, and a number a reviewer cannot
    reconstruct is a number they are right not to trust.

    The letter-height requirements are stated even though this version does
    not measure letters. Knowing the target is most of the value, and the
    remark is explicit that it is a requirement rather than a measurement.
    """
    try:
        area = metrology.pdp_area_cm2(spec)
    except metrology.MissingDimension as exc:
        # A rectangular pack gets its panel from the artwork when the reviewer
        # leaves it blank, so reaching here means that failed too. Saying so
        # saves them looking for a setting that would have prevented it.
        artwork = (
            ' The artwork does not declare one either: no "Size" line was '
            'found in a FOR INTERNAL USE block.'
            if spec.shape == 'RECTANGULAR' else '')
        return 'FAIL', (
            f'The Principal Display Panel area could not be computed: {exc}'
            f'{artwork} Every dimensional check is measured against the PDP, '
            'so supply the package dimensions and run the check again.')

    container = ('blown, formed, moulded or perforated'
                 if spec.container == 'BLOWN' else 'normal')
    fssai = metrology.required_letter_height_mm(area, spec,
                                                metrology.FSSAI_HEIGHT)
    legal = metrology.required_letter_height_mm(area, spec,
                                                metrology.LM_HEIGHT)

    remarks = [
        'Basis for the dimensional checks, not a verdict on the artwork.',
        _describe_basis(spec, area),
        f'Container treated as {container}.',
        f'On this PDP the minimum height of letters and numerals is '
        f'{fssai} mm for general FSSAI declarations and {legal} mm for net '
        f'quantity, retail sale price, expiry and consumer-care details '
        f'(Legal Metrology). Letter heights on the artwork are NOT measured '
        f'by this version — check them against those figures by eye.',
        f'Width of any letter or numeral must be at least one third of its '
        f'height, except 1, i, I and l.',
    ]
    if spec.panel_source:
        how = ('read from the artwork text'
               if spec.panel_source == 'DECLARED'
               else 'read by OCR from the artwork, which can misread a digit')
        assumed = (' The block gave no unit, so centimetres were assumed.'
                   if spec.panel_unit_assumed else '')
        remarks.append(
            f'No panel size was entered, so {spec.panel_width_mm} x '
            f'{spec.panel_height_mm} mm was {how} — the "Size" line in the '
            f'FOR INTERNAL USE block.{assumed} Confirm it is the largest '
            'panel, and enter the dimensions by hand if it is not.')

    return 'PASS', ' '.join(remarks)


def check_small_package(spec, image, params) -> tuple:
    """The <= 10 cm3 exemption, stated when it applies.

    Only reached when `_applicable` agrees the pack is small, so this is
    always the same explanation. It exists as a finding so the exemption is
    on the record: a reviewer looking at a report with no PDP area needs to
    see WHY there is none.
    """
    capacity = (f'{spec.capacity_cm3} cm3' if spec.capacity_cm3
                else '10 cm3 or less')
    return 'PASS', (
        f'This package ({capacity}) may carry its Principal Display Panel on '
        'a card or tape instead of the package itself, provided the card or '
        'tape is firmly affixed and carries the required information. Confirm '
        'by eye that it is attached and legible; there is no panel area to '
        'compute for a package this size.')


def check_veg_mark(spec, image, params) -> tuple:
    """The veg/non-veg mark against the size table for this PDP."""
    try:
        area = metrology.pdp_area_cm2(spec)
    except metrology.MissingDimension as exc:
        return 'FAIL', (
            f'The mark size could not be checked because the PDP is unknown: '
            f'{exc}')

    if spec.veg_mark_mm is None:
        return 'FAIL', (
            'A vegetarian/non-vegetarian mark was declared but its measured '
            'size was not entered, so it cannot be checked against the table. '
            'Measure the mark on the artwork and enter it.')

    shape = spec.veg_mark_shape
    required = metrology.required_mark_size_mm(area, shape)
    dimension = metrology.MARK_DIMENSION_NAME[shape]
    kind = 'non-vegetarian' if spec.veg_mark == 'NON_VEG' else 'vegetarian'
    preamble = (f'{kind.capitalize()} mark, {shape.lower()}, measured '
                f'{dimension} {spec.veg_mark_mm} mm; the minimum for a PDP of '
                f'{area:.1f} cm2 is {required} mm.')

    if spec.veg_mark_mm + 1e-9 >= required:
        return 'PASS', (
            f'{preamble} The mark meets the prescribed minimum. The size was '
            'entered by the reviewer, not measured off the artwork, so it is '
            'only as good as that measurement.')

    shortfall = required - spec.veg_mark_mm
    return 'FAIL', (
        f'{preamble} It is {shortfall:.2f} mm under the minimum and must be '
        f'enlarged to at least {required} mm.')


def check_fort_logo_size(spec, image, params) -> tuple:
    """The fortification logo's overall size, and the artwork set it implies.

    Only A and B are judged. The published drawing breaks the logo into
    C, D, a, E, F and G as well, but checking those independently would mean
    registering the official artwork against this label — and the drawing's
    own instruction is that the logo is scaled PROPORTIONATELY rather than
    element by element. So the finding reports the full set for the measured
    size and says plainly that those are asserted by proportion, not measured.
    """
    if spec.fort_a_mm is None or spec.fort_b_mm is None:
        return 'FAIL', (
            'This product was declared as fortified but the logo dimensions '
            'A and B were not entered, so the logo cannot be checked against '
            'the prescribed artwork. Measure the overall width and height of '
            'the logo box and enter them.')

    difference = abs(spec.fort_a_mm - spec.fort_b_mm)
    if difference > metrology.SQUARENESS_TOLERANCE_MM:
        return 'FAIL', (
            f'The logo box measures A={spec.fort_a_mm} mm by '
            f'B={spec.fort_b_mm} mm, a difference of {difference:.2f} mm. The '
            'prescribed artwork is square at every size (A equals B in all '
            'five published rows), so this logo has been stretched. Rescale it '
            'proportionately rather than adjusting one axis.')

    dims, prescribed = metrology.fortification_dimensions(spec.fort_a_mm)
    detail = metrology.format_dimensions(dims)

    if prescribed:
        size = metrology.nearest_prescribed_size(spec.fort_a_mm)
        return 'PASS', (
            f'The logo box measures {spec.fort_a_mm} x {spec.fort_b_mm} mm, '
            f'matching the prescribed {size:g} mm size. At that size the '
            f'published artwork is {detail}. Those sub-element dimensions are '
            'the published values for this size, not measurements of this '
            'artwork — check the logo against them by eye. The tagline '
            '"Sampoorna Poshan Swastha Jeevan" may be placed under the logo.')

    return 'PASS', (
        f'The logo box measures {spec.fort_a_mm} x {spec.fort_b_mm} mm, which '
        'is square but is not one of the five prescribed sizes (20, 40, 80, '
        '160 or 320 mm). The drawing scales proportionately, so at this size '
        f'the artwork should be {detail} — derived by scaling the 20 mm row, '
        'not published values. Confirm the logo was scaled as a whole and '
        'that "Fortified with", the fortificant name and the logo all appear.')


def check_fort_logo_colour(spec, image, params) -> tuple:
    """The two specified logo colours, measured off the artwork.

    Honest about its own reach: this finds the colours ANYWHERE on the label,
    because nothing here locates the logo. Blue is the diagnostic half —
    PANTONE 3005 C is not a colour that turns up by accident. Black is
    reported for completeness and explicitly not judged, because every label
    is covered in black text and a PASS on it would mean nothing.
    """
    if image is None:
        return 'FAIL', 'The artwork could not be read, so colour was not measured.'

    tolerance = float((params or {}).get('tolerance_de')
                      or metrology.COLOUR_TOLERANCE_DE)

    try:
        blue = metrology.measure_colour(image, metrology.FORT_BLUE, tolerance)
        black = metrology.measure_colour(image, metrology.FORT_BLACK, tolerance)
    except Exception as exc:  # noqa: BLE001 — numpy/Pillow raise their own
        logger.exception('Colour measurement failed')
        return 'FAIL', f'Colour could not be measured on this artwork: {exc}'

    scope = ('Measured across the whole artwork, not within the logo: this '
             'version does not locate the logo, so it confirms the colour is '
             'on the label rather than that the logo is printed in it.')
    black_note = (
        f'PANTONE BLACK ({metrology.FORT_BLACK}): closest ink found '
        f'{black.closest_hex}, Delta-E {black.delta_e:.1f}. Reported only — '
        'every label carries black text, so this cannot distinguish the logo.')

    if blue.present:
        return 'PASS', (
            f'PANTONE 3005 C ({metrology.FORT_BLUE}) is present: closest ink '
            f'found {blue.closest_hex}, Delta-E {blue.delta_e:.1f} against a '
            f'tolerance of {tolerance:.0f}, covering {blue.share:.2%} of the '
            f'artwork. {black_note} {scope}')

    if blue.share < metrology.COLOUR_MIN_SHARE and blue.delta_e <= tolerance:
        reason = (f'only a trace of it appears ({blue.share:.3%} of the '
                  'artwork), which is closer to edge artefacts than to printed '
                  'ink')
    else:
        reason = (f'the nearest ink on the label is {blue.closest_hex}, '
                  f'Delta-E {blue.delta_e:.1f} against a tolerance of '
                  f'{tolerance:.0f}')

    return 'FAIL', (
        f'PANTONE 3005 C ({metrology.FORT_BLUE}) was not found on this '
        f'artwork: {reason}. The fortification logo must be printed in the '
        f'specified blue (C-100 M-46 Y-2 K-0, R-0 G-116 B-200). {black_note} '
        f'{scope}')


#: Code -> checker. A dict rather than a chain of ifs so that adding the
#: letter-height checks later is one entry and one function.
CHECKS = {
    PDP_AREA: check_pdp_area,
    SMALL_PACKAGE_PDP: check_small_package,
    VEG_MARK_SIZE: check_veg_mark,
    FORT_LOGO_SIZE: check_fort_logo_size,
    FORT_LOGO_COLOUR: check_fort_logo_colour,
}


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def run_dimension_checks(rules, spec, image) -> tuple:
    """Run every measurement rule in `rules`. Returns `(findings, skipped)`.

    `rules` is the measurement subset in checklist order, `spec` the reviewer's
    `metrology.PackageSpec`, `image` the rasterised artwork.

    `skipped` is a list of `{'rule_id', 'rule_name', 'reason'}` — rules that do
    not apply to this pack. The caller reports them; see the module docstring
    for why they are not findings.

    One check failing does not stop the others. They are independent questions
    about the same pack, and losing four answers because the fifth raised would
    be a poor trade.
    """
    findings = []
    skipped = []

    for rule in rules:
        reason = _applicable(rule.code, spec)
        if reason:
            skipped.append({'rule_id': rule.code, 'rule_name': rule.name,
                            'reason': reason})
            continue

        checker = CHECKS.get(rule.code)
        if checker is None:
            # Active, marked MEASUREMENT, and unknown to this version. Said
            # out loud rather than dropped: a rule the legal desk switched on
            # and that silently does nothing is the worst possible outcome.
            findings.append(RuleFinding(
                rule_id=rule.code, rule_name=rule.name, status='FAIL',
                remarks=('This rule is marked as a measurement check but no '
                         'measurement is implemented for the code '
                         f'"{rule.code}" in this version. Either deactivate '
                         'it or change its type to an AI rule.'),
            ))
            continue

        try:
            status, remarks = checker(spec, image, rule.params or {})
        except Exception as exc:  # noqa: BLE001 — a checker bug, not the label's
            logger.exception('Dimension check %s failed', rule.code)
            status, remarks = 'FAIL', (
                f'This check could not be completed because of an internal '
                f'error: {exc}')

        findings.append(RuleFinding(
            rule_id=rule.code, rule_name=rule.name, status=status,
            remarks=remarks,
        ))

    return findings, skipped
