"""Tests for the dimensional checks.

Plain `unittest`, not `django.test.TestCase`, and that is deliberate twice
over. `metrology` and `dimensions` import no Django at all — tables,
arithmetic and pixels — so a test that spun up a database to exercise them
would be testing the harness. And the rules these run against are passed in as
objects, so nothing here needs a row.

What is actually worth pinning
------------------------------
The table BOUNDARIES, above all. "Up to 200" and "above 200 to 500" is one
reading of a published table and `>= 200` is another, and the difference is a
label that passes at exactly 200 cm2 under one and fails under the other.
Every boundary in every table is therefore tested on the value itself, not
near it.

Then the two places the rules do something other than a lookup: the FSSAI /
Legal Metrology disagreement about irregular shapes, and the fortification
logo's scaling.
"""
import unittest

from . import dimensions, metrology


class Rule:
    """The minimum `run_dimension_checks` reads off a `ComplianceRule`."""

    def __init__(self, code, name='', params=None):
        self.code = code
        self.name = name or code
        self.params = params or {}


def spec(**changes):
    """A rectangular 100 x 100 mm pack — PDP 400 cm2 — with `changes`."""
    base = {'shape': 'RECTANGULAR', 'panel_height_mm': 100.0,
            'panel_width_mm': 100.0}
    base.update(changes)
    return metrology.PackageSpec(**base)


# ---------------------------------------------------------------------------
# PDP area
# ---------------------------------------------------------------------------

class PdpAreaTests(unittest.TestCase):

    def test_rectangular_is_40_percent_of_the_largest_panel(self):
        # 40% of 100 mm x 100 mm = 4000 mm2 = 40 cm2.
        self.assertAlmostEqual(metrology.pdp_area_cm2(spec()), 40.0)

    def test_cylindrical_uses_height_by_average_circumference(self):
        area = metrology.pdp_area_cm2(metrology.PackageSpec(
            shape='CYLINDRICAL', height_mm=80.0, circumference_mm=200.0))
        # 40% of 80 x 200 mm = 6400 mm2 = 64 cm2.
        self.assertAlmostEqual(area, 64.0)

    def test_other_shape_differs_between_the_two_extracts(self):
        """The disagreement in section 1, pinned so it cannot be "tidied up".

        FSSAI says 20% of total surface area for an irregular pack; Legal
        Metrology says 40% of it. Both are transcribed correctly and the
        applicable one is an input. A future reader who "fixes" one of them
        breaks this test, which is the entire point of it.
        """
        fssai = metrology.pdp_area_cm2(metrology.PackageSpec(
            shape='OTHER', surface_area_cm2=1000.0, regulation='FSSAI'))
        legal = metrology.pdp_area_cm2(metrology.PackageSpec(
            shape='OTHER', surface_area_cm2=1000.0,
            regulation='LEGAL_METROLOGY'))
        self.assertAlmostEqual(fssai, 200.0)
        self.assertAlmostEqual(legal, 400.0)

    def test_missing_dimensions_name_what_is_missing(self):
        with self.assertRaises(metrology.MissingDimension) as caught:
            metrology.pdp_area_cm2(metrology.PackageSpec(shape='RECTANGULAR'))
        self.assertIn('largest panel', str(caught.exception))

    def test_no_shape_is_a_missing_dimension_not_a_crash(self):
        with self.assertRaises(metrology.MissingDimension):
            metrology.pdp_area_cm2(metrology.PackageSpec())

    def test_small_package_has_no_computed_area(self):
        with self.assertRaises(metrology.MissingDimension) as caught:
            metrology.pdp_area_cm2(metrology.PackageSpec(shape='SMALL'))
        self.assertIn('card or tape', str(caught.exception))

    def test_small_package_recognised_by_capacity_or_by_shape(self):
        self.assertTrue(metrology.is_small_package(
            metrology.PackageSpec(shape='SMALL')))
        # 10 cm3 exactly is INSIDE the exemption.
        self.assertTrue(metrology.is_small_package(spec(capacity_cm3=10.0)))
        self.assertFalse(metrology.is_small_package(spec(capacity_cm3=10.5)))


# ---------------------------------------------------------------------------
# The tables
# ---------------------------------------------------------------------------

class LetterHeightTableTests(unittest.TestCase):
    """Section 2 (FSSAI) and section 9 (Legal Metrology)."""

    def height(self, area, container='NORMAL', table=metrology.FSSAI_HEIGHT):
        return metrology.required_letter_height_mm(
            area, spec(container=container), table)

    def test_fssai_boundaries_are_inclusive_upper_bounds(self):
        self.assertEqual(self.height(200.0), 1.0)     # "up to 200"
        self.assertEqual(self.height(200.01), 2.0)    # "above 200 to 500"
        self.assertEqual(self.height(500.0), 2.0)
        self.assertEqual(self.height(500.01), 3.0)
        self.assertEqual(self.height(2500.0), 3.0)
        self.assertEqual(self.height(2500.01), 6.0)

    def test_fssai_blown_containers_take_the_second_column(self):
        self.assertEqual(self.height(600.0), 3.0)
        self.assertEqual(self.height(600.0, 'BLOWN'), 5.0)

    def test_legal_metrology_boundaries(self):
        table = metrology.LM_HEIGHT
        self.assertEqual(self.height(50.0, table=table), 1.0)
        self.assertEqual(self.height(50.01, table=table), 2.0)
        self.assertEqual(self.height(100.0, table=table), 2.0)
        self.assertEqual(self.height(100.01, table=table), 2.5)
        self.assertEqual(self.height(500.0, table=table), 2.5)
        self.assertEqual(self.height(2500.0, table=table), 4.0)
        self.assertEqual(self.height(2500.01, table=table), 6.0)

    def test_legal_metrology_top_row_is_6mm_for_both_containers(self):
        """Transcribed as published, not "corrected" to match the pattern.

        Every other row makes the blown requirement larger. This one does not,
        and a reader who assumes the pattern would quietly raise the bar on
        the largest packs.
        """
        table = metrology.LM_HEIGHT
        self.assertEqual(self.height(3000.0, table=table), 6.0)
        self.assertEqual(self.height(3000.0, 'BLOWN', table=table), 6.0)


class VegMarkTableTests(unittest.TestCase):
    """Section 5."""

    def test_each_shape_reads_its_own_column(self):
        self.assertEqual(metrology.required_mark_size_mm(700.0, 'CIRCLE'), 6.0)
        self.assertEqual(metrology.required_mark_size_mm(700.0, 'TRIANGLE'), 5.0)
        self.assertEqual(metrology.required_mark_size_mm(700.0, 'SQUARE'), 12.0)

    def test_boundaries_are_inclusive_upper_bounds(self):
        self.assertEqual(metrology.required_mark_size_mm(100.0, 'CIRCLE'), 3.0)
        self.assertEqual(metrology.required_mark_size_mm(100.01, 'CIRCLE'), 4.0)
        self.assertEqual(metrology.required_mark_size_mm(500.0, 'CIRCLE'), 4.0)
        self.assertEqual(metrology.required_mark_size_mm(500.01, 'CIRCLE'), 6.0)
        self.assertEqual(metrology.required_mark_size_mm(2500.0, 'CIRCLE'), 6.0)
        self.assertEqual(metrology.required_mark_size_mm(2500.01, 'CIRCLE'), 8.0)


class FortificationGeometryTests(unittest.TestCase):
    """Section 7."""

    def test_a_prescribed_size_returns_the_published_row_verbatim(self):
        dims, prescribed = metrology.fortification_dimensions(80.0)
        self.assertTrue(prescribed)
        # 8.9, not 2 x 4.4 — the published values are rounded at each size.
        self.assertEqual(dims['C'], 8.9)
        self.assertEqual(dims['G'], (67.92, 43.75))

    def test_a_size_close_enough_counts_as_prescribed(self):
        self.assertEqual(metrology.nearest_prescribed_size(40.5), 40.0)
        self.assertIsNone(metrology.nearest_prescribed_size(50.0))

    def test_an_off_table_size_is_scaled_from_the_20mm_row(self):
        dims, prescribed = metrology.fortification_dimensions(10.0)
        self.assertFalse(prescribed)
        self.assertEqual(dims['C'], 1.1)           # 2.2 / 2
        # 7.27 / 2 = 3.635 and 9.51 / 2 = 4.755, both rounded to two places.
        # The half-way cases go down, which is `round`'s documented behaviour
        # and is fine here: a derived dimension is a guide to check artwork
        # against, and a hundredth of a millimetre is below anyone's ruler.
        self.assertEqual(dims['E'], (3.63, 4.75))

    def test_every_published_row_is_square(self):
        """A is the key and B equals it; the check for stretch depends on it."""
        for size in metrology.FORT_LOGO_SIZES:
            self.assertIn(size, (20.0, 40.0, 80.0, 160.0, 320.0))


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

class PackageSpecTests(unittest.TestCase):

    def test_blank_and_unparseable_fields_become_none(self):
        parsed = metrology.PackageSpec.from_request({
            'package_shape': 'rectangular', 'panel_width_mm': '',
            'panel_height_mm': 'about 100', 'veg_mark_mm': '4.5',
        })
        self.assertEqual(parsed.shape, 'RECTANGULAR')
        self.assertIsNone(parsed.panel_width_mm)
        self.assertIsNone(parsed.panel_height_mm)
        self.assertEqual(parsed.veg_mark_mm, 4.5)

    def test_a_zero_or_negative_dimension_is_treated_as_absent(self):
        parsed = metrology.PackageSpec.from_request({'panel_width_mm': '0'})
        self.assertIsNone(parsed.panel_width_mm)

    def test_an_unknown_choice_falls_back_rather_than_raising(self):
        parsed = metrology.PackageSpec.from_request({
            'package_shape': 'trapezoid', 'container_type': 'squeezed'})
        self.assertEqual(parsed.shape, '')
        self.assertEqual(parsed.container, 'NORMAL')

    def test_configured_is_false_for_an_untouched_form(self):
        self.assertFalse(metrology.PackageSpec.from_request({}).configured)
        self.assertTrue(spec().configured)

    def test_a_declared_size_only_fills_a_gap(self):
        declared = metrology.DeclaredSize(26.0, 80.0, False, 'DECLARED')

        filled = metrology.PackageSpec(
            shape='RECTANGULAR').with_declared_panel(declared)
        self.assertEqual(filled.panel_width_mm, 26.0)
        self.assertEqual(filled.panel_source, 'DECLARED')

        # A reviewer who typed the panel keeps what they typed.
        typed = spec().with_declared_panel(declared)
        self.assertEqual(typed.panel_width_mm, 100.0)
        self.assertEqual(typed.panel_source, '')

        # A non-rectangular pack has no panel to fill.
        cylinder = metrology.PackageSpec(
            shape='CYLINDRICAL').with_declared_panel(declared)
        self.assertIsNone(cylinder.panel_width_mm)

        # Nothing declared, nothing invented.
        empty = metrology.PackageSpec(
            shape='RECTANGULAR').with_declared_panel(None)
        self.assertIsNone(empty.panel_width_mm)


class DeclaredSizeTests(unittest.TestCase):
    """The "Size" line in the artwork's FOR INTERNAL USE block.

    This is where the panel comes from in practice, so the parser has to
    survive both the neat vector text and what OCR makes of the same line.
    """

    def parse(self, text):
        return metrology.parse_declared_size(text)

    def test_a_labelled_unit_is_used(self):
        size = self.parse('Size(cm): 2.6 x 8')
        self.assertEqual((size.width_mm, size.height_mm), (26.0, 80.0))
        self.assertFalse(size.unit_assumed)

    def test_millimetres_and_inches_are_honoured(self):
        self.assertEqual(self.parse('Size (mm): 130 x 42').width_mm, 130.0)
        self.assertAlmostEqual(self.parse('Size(in): 2 x 1').width_mm, 50.8)

    def test_a_bare_size_is_read_as_centimetres_and_says_so(self):
        """13 x 4.2 mm would be smaller than the licence number on the pack."""
        size = self.parse('FOR INTERNAL USE   Size: 13 x 4.2')
        self.assertEqual((size.width_mm, size.height_mm), (130.0, 42.0))
        self.assertTrue(size.unit_assumed)

    def test_ocr_damage_is_tolerated(self):
        for text in ('Size(cm) 2.6 X 8', 'Size: 2.6 * 8', 'Size - 2.6 x 8',
                     'Size(cm):  2.6  ×  8'):
            with self.subTest(text=text):
                size = self.parse(text)
                self.assertIsNotNone(size)
                self.assertEqual(size.width_mm, 26.0)

    def test_a_serving_size_is_not_a_pack_size(self):
        self.assertIsNone(self.parse('Serving Size: 2 x 15 ml'))

    def test_the_internal_use_block_wins_over_an_earlier_match(self):
        text = ('Type Size: 8 x 10\n'
                'FOR INTERNAL USE\n'
                'Size(cm): 2.6 x 8')
        self.assertEqual(self.parse(text).width_mm, 26.0)

    def test_nothing_found_is_none_not_a_guess(self):
        self.assertIsNone(self.parse('Net Quantity 1 L'))
        self.assertIsNone(self.parse(''))

    def test_the_source_is_carried_through(self):
        size = metrology.parse_declared_size('Size(cm): 2.6 x 8', 'DECLARED_OCR')
        self.assertEqual(size.source, 'DECLARED_OCR')


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def swatch(hex_colour, size=(40, 40)):
    """A solid image of one colour."""
    from PIL import Image
    return Image.new('RGB', size, metrology.hex_to_rgb(hex_colour))


class ColourTests(unittest.TestCase):

    def test_the_exact_colour_measures_as_zero_distance(self):
        match = metrology.measure_colour(swatch(metrology.FORT_BLUE),
                                         metrology.FORT_BLUE)
        self.assertAlmostEqual(match.delta_e, 0.0, places=3)
        self.assertEqual(match.closest_hex, metrology.FORT_BLUE)
        self.assertTrue(match.present)

    def test_an_unrelated_colour_is_far_away(self):
        match = metrology.measure_colour(swatch('#FF0000'),
                                         metrology.FORT_BLUE)
        self.assertGreater(match.delta_e, metrology.COLOUR_TOLERANCE_DE)
        self.assertFalse(match.present)

    def test_a_small_patch_of_the_colour_is_still_found(self):
        """The logo is a small part of a label — that is the normal case."""
        from PIL import Image
        image = Image.new('RGB', (200, 200), (255, 255, 255))
        image.paste(swatch(metrology.FORT_BLUE, (20, 20)), (10, 10))
        match = metrology.measure_colour(image, metrology.FORT_BLUE)
        self.assertAlmostEqual(match.delta_e, 0.0, places=3)
        self.assertTrue(match.present)

    def test_a_few_stray_pixels_do_not_count_as_present(self):
        from PIL import Image
        image = Image.new('RGB', (400, 400), (255, 255, 255))
        image.paste(swatch(metrology.FORT_BLUE, (2, 2)), (10, 10))
        match = metrology.measure_colour(image, metrology.FORT_BLUE)
        # Found, and correctly reported as found...
        self.assertAlmostEqual(match.delta_e, 0.0, places=3)
        # ...but too little of it to be ink.
        self.assertFalse(match.present)

    def test_hex_round_trips(self):
        self.assertEqual(metrology.hex_to_rgb('#0074C8'), (0, 116, 200))
        self.assertEqual(metrology.rgb_to_hex((0, 116, 200)), '#0074C8')


# ---------------------------------------------------------------------------
# The checks, as findings
# ---------------------------------------------------------------------------

class VegMarkCheckTests(unittest.TestCase):

    def check(self, **changes):
        base = {'veg_mark': 'VEG', 'veg_mark_shape': 'CIRCLE',
                'veg_mark_mm': 4.0}
        base.update(changes)
        return dimensions.check_veg_mark(spec(**base), None, {})

    def test_a_mark_at_the_minimum_passes(self):
        # PDP 40 cm2 -> circle minimum 3 mm.
        status, remarks = self.check(veg_mark_mm=3.0)
        self.assertEqual(status, 'PASS')
        self.assertIn('3.0 mm', remarks)

    def test_an_undersized_mark_fails_and_says_by_how_much(self):
        status, remarks = self.check(veg_mark_mm=2.0)
        self.assertEqual(status, 'FAIL')
        self.assertIn('1.00 mm under', remarks)

    def test_a_missing_measurement_fails_rather_than_passing_quietly(self):
        status, remarks = self.check(veg_mark_mm=None)
        self.assertEqual(status, 'FAIL')
        self.assertIn('not entered', remarks)

    def test_an_unknown_pdp_fails_with_the_reason(self):
        parsed = metrology.PackageSpec(shape='RECTANGULAR', veg_mark='VEG',
                                       veg_mark_mm=4.0)
        status, remarks = dimensions.check_veg_mark(parsed, None, {})
        self.assertEqual(status, 'FAIL')
        self.assertIn('largest panel', remarks)


class FortificationCheckTests(unittest.TestCase):

    def test_a_prescribed_square_logo_passes_and_reports_the_artwork_set(self):
        status, remarks = dimensions.check_fort_logo_size(
            spec(fortified=True, fort_a_mm=40.0, fort_b_mm=40.0), None, {})
        self.assertEqual(status, 'PASS')
        self.assertIn('C=4.4', remarks)
        self.assertIn('G=33.96x21.87', remarks)

    def test_a_stretched_logo_fails(self):
        status, remarks = dimensions.check_fort_logo_size(
            spec(fortified=True, fort_a_mm=40.0, fort_b_mm=44.0), None, {})
        self.assertEqual(status, 'FAIL')
        self.assertIn('stretched', remarks)

    def test_an_off_table_size_passes_but_says_the_numbers_are_derived(self):
        status, remarks = dimensions.check_fort_logo_size(
            spec(fortified=True, fort_a_mm=50.0, fort_b_mm=50.0), None, {})
        self.assertEqual(status, 'PASS')
        self.assertIn('not one of the five prescribed sizes', remarks)
        self.assertIn('not published values', remarks)

    def test_missing_dimensions_fail(self):
        status, remarks = dimensions.check_fort_logo_size(
            spec(fortified=True), None, {})
        self.assertEqual(status, 'FAIL')
        self.assertIn('were not entered', remarks)

    def test_colour_passes_on_artwork_carrying_the_blue(self):
        from PIL import Image
        image = Image.new('RGB', (200, 200), (255, 255, 255))
        image.paste(swatch(metrology.FORT_BLUE, (40, 40)), (10, 10))
        image.paste(swatch(metrology.FORT_BLACK, (40, 40)), (60, 10))
        status, remarks = dimensions.check_fort_logo_colour(
            spec(fortified=True), image, {})
        self.assertEqual(status, 'PASS')
        self.assertIn('PANTONE 3005 C', remarks)
        # The claim is scoped, not overstated.
        self.assertIn('whole artwork', remarks)

    def test_colour_fails_when_the_blue_is_absent(self):
        status, remarks = dimensions.check_fort_logo_colour(
            spec(fortified=True), swatch('#FFFFFF', (200, 200)), {})
        self.assertEqual(status, 'FAIL')
        self.assertIn('was not found', remarks)

    def test_the_tolerance_is_overridable_per_rule(self):
        """A wrong-ish blue passes only when the rule says it may."""
        near = '#1A7FCC'
        image = swatch(near, (200, 200))
        strict, _ = dimensions.check_fort_logo_colour(
            spec(fortified=True), image, {'tolerance_de': 0.5})
        loose, _ = dimensions.check_fort_logo_colour(
            spec(fortified=True), image, {'tolerance_de': 40})
        self.assertEqual(strict, 'FAIL')
        self.assertEqual(loose, 'PASS')


class PdpFindingTests(unittest.TestCase):

    def test_the_basis_finding_states_both_height_requirements(self):
        status, remarks = dimensions.check_pdp_area(spec(), None, {})
        self.assertEqual(status, 'PASS')
        self.assertIn('not a verdict', remarks)
        # PDP 40 cm2 -> FSSAI 1.0 mm, Legal Metrology 1.0 mm.
        self.assertIn('1.0 mm for general FSSAI', remarks)

    def test_a_panel_taken_from_the_artwork_is_flagged_as_such(self):
        parsed = metrology.PackageSpec(
            shape='RECTANGULAR').with_declared_panel(
                metrology.DeclaredSize(26.0, 80.0, False, 'DECLARED'))
        _status, remarks = dimensions.check_pdp_area(parsed, None, {})
        self.assertIn('FOR INTERNAL USE', remarks)
        self.assertIn('read from the artwork text', remarks)

    def test_an_ocr_read_panel_says_it_was_ocr(self):
        parsed = metrology.PackageSpec(
            shape='RECTANGULAR').with_declared_panel(
                metrology.DeclaredSize(130.0, 42.0, True, 'DECLARED_OCR'))
        _status, remarks = dimensions.check_pdp_area(parsed, None, {})
        self.assertIn('can misread a digit', remarks)
        self.assertIn('centimetres were assumed', remarks)

    def test_a_rectangular_pack_with_nothing_to_go_on_says_both_failed(self):
        status, remarks = dimensions.check_pdp_area(
            metrology.PackageSpec(shape='RECTANGULAR'), None, {})
        self.assertEqual(status, 'FAIL')
        self.assertIn('artwork does not declare one either', remarks)

    def test_missing_dimensions_fail_loudly(self):
        status, remarks = dimensions.check_pdp_area(
            metrology.PackageSpec(shape='CYLINDRICAL'), None, {})
        self.assertEqual(status, 'FAIL')
        self.assertIn('average circumference', remarks)


class RunDimensionChecksTests(unittest.TestCase):

    RULES = [Rule(code) for code in dimensions.SUPPORTED]

    def test_inapplicable_rules_are_skipped_with_a_reason_not_failed(self):
        findings, skipped = dimensions.run_dimension_checks(
            self.RULES, spec(), None)
        codes = {finding.rule_id for finding in findings}
        skipped_codes = {entry['rule_id'] for entry in skipped}

        # Not fortified and no mark declared: four of the five do not apply.
        self.assertEqual(codes, {dimensions.PDP_AREA})
        self.assertEqual(skipped_codes, {
            dimensions.SMALL_PACKAGE_PDP, dimensions.VEG_MARK_SIZE,
            dimensions.FORT_LOGO_SIZE, dimensions.FORT_LOGO_COLOUR})
        for entry in skipped:
            self.assertTrue(entry['reason'])

    def test_a_declared_mark_is_checked(self):
        findings, _ = dimensions.run_dimension_checks(
            self.RULES, spec(veg_mark='VEG', veg_mark_mm=2.0), None)
        by_code = {finding.rule_id: finding for finding in findings}
        self.assertIn(dimensions.VEG_MARK_SIZE, by_code)
        self.assertEqual(by_code[dimensions.VEG_MARK_SIZE].status, 'FAIL')

    def test_an_unimplemented_measurement_code_says_so(self):
        findings, skipped = dimensions.run_dimension_checks(
            [Rule('LETTER_HEIGHTS', 'Letter heights')], spec(), None)
        self.assertEqual(skipped, [])
        self.assertEqual(findings[0].status, 'FAIL')
        self.assertIn('no measurement is implemented', findings[0].remarks)

    def test_one_check_raising_does_not_lose_the_others(self):
        """A checker bug costs its own finding and nothing else."""
        original = dimensions.CHECKS[dimensions.PDP_AREA]

        def explode(*_args):
            raise RuntimeError('boom')

        dimensions.CHECKS[dimensions.PDP_AREA] = explode
        try:
            findings, _ = dimensions.run_dimension_checks(
                self.RULES, spec(veg_mark='VEG', veg_mark_mm=5.0), None)
        finally:
            dimensions.CHECKS[dimensions.PDP_AREA] = original

        by_code = {finding.rule_id: finding for finding in findings}
        self.assertEqual(by_code[dimensions.PDP_AREA].status, 'FAIL')
        self.assertIn('internal error', by_code[dimensions.PDP_AREA].remarks)
        # The mark was still checked, and still passed.
        self.assertEqual(by_code[dimensions.VEG_MARK_SIZE].status, 'PASS')

    def test_findings_carry_no_highlight_regions(self):
        """A dimension is not a place on the artwork.

        `attach_regions` is never run over these, and inventing a box for a
        number that came off a form would be the same mistake the model is
        kept away from.
        """
        findings, _ = dimensions.run_dimension_checks(
            self.RULES, spec(), None)
        for finding in findings:
            self.assertEqual(finding.regions, [])
            self.assertIsNone(finding.ocr_verified)


if __name__ == '__main__':
    unittest.main()
