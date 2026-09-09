"""The hybrid label-check pipeline, with Gemini and Tesseract mocked out.

What is worth testing here, and why:

1. **The response structure the frontend consumes.** `Label_Checker.tsx` maps
   `findings[].rule_id / rule_name / status / remarks` and `summary.*`
   straight into the report it renders. A field renamed or a status spelled
   `"Pass"` reaches the user as an empty checklist with no error, so the shape
   is pinned from the outside — through the URL, with the model mocked.

2. **The cross-reference actually cross-references.** It is the entire claim
   of a "hybrid" pipeline. If it silently passed the model's verdicts through,
   every test of the endpoint would still be green and the feature would be an
   expensive single API call. Each branch is exercised directly.

3. **Degradation is real.** No rules configured, and Tesseract missing, are
   both states a live host will be in. The first must be a message; the second
   must still produce a report.

Nothing here touches the network. `call_gemini` is patched at the seam — the
one function that owns the SDK — so the tests say nothing about how the SDK is
called, only about what the pipeline does with an answer.
"""
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from users.models import User

from . import service
from .models import ComplianceRule, LabelData
from .ocr import (OcrRead, Word, contains as ocr_contains,
                  fssai_numbers as ocr_fssai_numbers, locate as ocr_locate)
from .schemas import GeminiLabelReport, GeminiRuleFinding, RuleFinding

UPLOAD_URL = '/api/legal/upload/'
RULES_URL = '/api/legal/rules/'


def gemini_reply(*findings) -> GeminiLabelReport:
    """A model response, as the SDK hands it back after schema validation.

    Each finding is `(rule_id, rule_name, status, remarks)` or the same plus
    `evidence_text` — the wording the model says it read, which is what
    `attach_regions` looks for on the page.
    """
    return GeminiLabelReport(findings=[
        GeminiRuleFinding(rule_id=row[0], rule_name=row[1], status=row[2],
                          remarks=row[3],
                          evidence_text=row[4] if len(row) > 4 else '')
        for row in findings
    ])


def png_bytes(size=(8, 8)) -> bytes:
    """A real PNG, encoded by Pillow.

    Generated rather than pasted as a byte literal so that `load_image` is
    exercised for real — a hand-written literal that Pillow rejects would make
    every view test fail for a reason that has nothing to do with the view.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', size, (255, 255, 255)).save(buffer, format='PNG')
    return buffer.getvalue()


def fake_read(text='', words=(), size=(600, 200)) -> OcrRead:
    """An `OcrRead` for tests that care about the text, not the pixels.

    `service.run_label_check` reads boxes and text from one Tesseract pass, so
    a test that stubs OCR has to supply both. Most only care about the text —
    `words=()` means "nothing to highlight", which is a real state (a scan
    Tesseract could not box) and not a special case.
    """
    return OcrRead(text=text, words=tuple(words), size=size)


def word(text, left, top, width=40, height=10, line=(1, 1, 1)) -> Word:
    return Word(text=text, left=left, top=top, width=width, height=height,
                line=line)


def png_upload(name='label.png'):
    return SimpleUploadedFile(name, png_bytes(), content_type='image/png')


class CrossReferenceTests(TestCase):
    """The deterministic half — no HTTP, no model, just the logic."""

    def _rule(self, code='R1', tokens=None, critical=False):
        return ComplianceRule(
            code=code, name=f'Rule {code}', rule_text='...',
            critical_tokens=tokens or [], is_critical=critical)

    def _finding(self, code='R1', status='PASS', remarks='Found it.'):
        return GeminiRuleFinding(rule_id=code, rule_name=f'Rule {code}',
                                 status=status, remarks=remarks)

    def test_a_rule_with_no_tokens_gets_no_opinion(self):
        """Most rules are judgements OCR cannot corroborate. Claiming it did
        would be a lie of omission, so the field stays None."""
        rule = self._rule()

        [result] = service.cross_reference([self._finding()], [rule], 'any text')

        self.assertIsNone(result.ocr_verified)
        self.assertEqual(result.status, 'PASS')

    def test_a_present_token_verifies_the_finding(self):
        rule = self._rule(tokens=['Best Before'])

        [result] = service.cross_reference(
            [self._finding()], [rule], 'BEST  BEFORE: 12 months from mfg')

        self.assertTrue(result.ocr_verified)

    def test_matching_survives_ocr_noise(self):
        """Punctuation and doubled spaces are what OCR produces, not evidence
        that a declaration is absent."""
        rule = self._rule(tokens=['Best Before'])

        [result] = service.cross_reference(
            [self._finding()], [rule], 'best-before  ::  01/2027')

        self.assertTrue(result.ocr_verified)

    def test_a_missing_token_on_a_critical_rule_overturns_a_pass(self):
        """The point of the hybrid: the model said yes, the text says no, and
        for a statutory declaration that disagreement resolves to FAIL."""
        rule = self._rule(tokens=['FSSAI'], critical=True)

        [result] = service.cross_reference(
            [self._finding()], [rule], 'ingredients: refined oil')

        self.assertEqual(result.status, 'FAIL')
        self.assertFalse(result.ocr_verified)
        self.assertIn('overturned to FAIL', result.remarks)

    def test_a_missing_token_on_a_normal_rule_only_annotates(self):
        """OCR misses print constantly. Failing every unverified rule would
        bury the reviewer, so a non-critical rule keeps the model's verdict."""
        rule = self._rule(tokens=['Best Before'], critical=False)

        [result] = service.cross_reference(
            [self._finding()], [rule], 'ingredients: refined oil')

        self.assertEqual(result.status, 'PASS')
        self.assertFalse(result.ocr_verified)
        self.assertIn('could not find', result.remarks)

    def test_a_fail_whose_text_is_present_is_flagged_for_review(self):
        """The other direction of disagreement: the model may still be right
        (present but wrong, or in the wrong place), so the verdict stands and
        the reviewer is told to look."""
        rule = self._rule(tokens=['MRP'], critical=True)

        [result] = service.cross_reference(
            [self._finding(status='FAIL')], [rule], 'MRP Rs 250')

        self.assertEqual(result.status, 'FAIL')
        self.assertTrue(result.ocr_verified)
        self.assertIn('WAS found', result.remarks)

    def test_no_ocr_text_means_no_verification_at_all(self):
        """When Tesseract is missing there is nothing to cross-reference
        against, and a critical rule must NOT be overturned on that basis."""
        rule = self._rule(tokens=['FSSAI'], critical=True)

        [result] = service.cross_reference([self._finding()], [rule], '')

        self.assertEqual(result.status, 'PASS')
        self.assertIsNone(result.ocr_verified)


class TokenMatchingTests(TestCase):
    """`ocr.contains` — tolerant of OCR noise, intolerant of wrong words.

    These cases come from a real Tesseract run, not from imagination: on a
    synthetic label reading "FSSAI Lic. No. 10012345678901", Tesseract
    returned "FSSA Lic. No, 10012345678901". An exact match reported the
    licence declaration as absent, which would have overturned a correct PASS
    into a FAIL on the most important rule on the label.

    The second half matters as much as the first. Fuzziness that matched
    anything would make `ocr_verified` meaningless and would stop the
    cross-reference ever catching a genuinely missing declaration.
    """

    OCR_TEXT = 'JNOCANOLAOIL1.LTR FSSA Lic. No, 10012345678901 MRP Ps 250 (Incl ofall taxes)'

    def test_matches_a_token_with_a_dropped_character(self):
        self.assertTrue(ocr_contains(self.OCR_TEXT, 'FSSAI'))

    def test_matches_across_punctuation_and_spacing_noise(self):
        self.assertTrue(ocr_contains('best-before  ::  01/2027', 'Best Before'))

    def test_does_not_match_an_absent_declaration(self):
        for token in ('Allergen', 'Batch No', 'Country of Origin', 'Use By'):
            self.assertFalse(ocr_contains(self.OCR_TEXT, token), token)

    def test_short_tokens_are_matched_exactly(self):
        """At three characters a single substitution is a 0.67 ratio and two
        unrelated words are indistinguishable, so fuzziness only adds noise."""
        self.assertFalse(ocr_contains('the mrq is 250', 'MRP'))
        self.assertTrue(ocr_contains('the mrp is 250', 'MRP'))

    def test_a_blank_token_never_matches(self):
        self.assertFalse(ocr_contains('anything at all', '   '))

    def test_fssai_numbers_survive_the_surrounding_noise(self):
        """The digits are read reliably even where the words are not — which
        is why `build_prompt` hands them to the model as a verified fact."""
        self.assertEqual(ocr_fssai_numbers(self.OCR_TEXT), ['10012345678901'])

    def test_a_thirteen_digit_number_is_not_a_licence(self):
        self.assertEqual(ocr_fssai_numbers('Lic No 1001234567890 batch'), [])


class LocateTests(TestCase):
    """`ocr.locate` — boxes come from Tesseract, never from the model.

    The guarantee being protected: a highlight is drawn only where the words
    were actually read. The model supplies a quotation, not a coordinate, so
    the worst it can do is quote text that is not on the label — and then
    nothing is found and nothing is drawn, which these tests pin.
    """

    # Two printed lines, at known pixel positions on a 200x100 page.
    WORDS = (
        word('FSSAI', 20, 10, width=50, height=10, line=(1, 1, 1)),
        word('10012345678901', 75, 10, width=100, height=10, line=(1, 1, 1)),
        word('Best', 20, 60, width=40, height=10, line=(2, 1, 1)),
        word('Before', 65, 60, width=50, height=10, line=(2, 1, 1)),
    )
    SIZE = (200, 100)

    def test_locates_a_quoted_phrase(self):
        [box] = ocr_locate('Best Before', self.WORDS, self.SIZE)

        # x spans 20..115 of 200, y spans 60..70 of 100, plus 2px padding.
        self.assertAlmostEqual(box['x'], 18 / 200, places=3)
        self.assertAlmostEqual(box['y'], 58 / 100, places=3)
        self.assertGreater(box['width'], 0.4)

    def test_finds_nothing_for_an_absent_phrase(self):
        """A FAIL for a missing declaration has nowhere to point, and the
        honest answer is no box rather than a plausible one."""
        self.assertEqual(
            ocr_locate('Allergen information', self.WORDS, self.SIZE), [])

    def test_tolerates_the_ocr_misreading_the_quotation(self):
        """The model quotes what is PRINTED; Tesseract reports what it READ.
        Requiring those to match exactly would find nothing on a real scan."""
        misread = (word('FSSA', 20, 10, width=50, height=10),)

        self.assertTrue(ocr_locate('FSSAI', misread, self.SIZE))

    def test_a_phrase_spanning_two_lines_gets_a_box_per_line(self):
        """One rectangle from the start of a wrapped phrase to its end would
        cover everything printed between them."""
        boxes = ocr_locate('10012345678901 Best', self.WORDS, self.SIZE)

        self.assertEqual(len(boxes), 2)
        self.assertLess(boxes[0]['y'], boxes[1]['y'])

    def test_boxes_are_fractions_of_the_image(self):
        """The browser renders a downscaled preview at an arbitrary width, so
        a pixel coordinate would be wrong at every size but one."""
        for box in ocr_locate('FSSAI', self.WORDS, self.SIZE):
            for value in box.values():
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_no_words_means_no_boxes(self):
        self.assertEqual(ocr_locate('anything', (), self.SIZE), [])

    # -- The sub-phrase ladder ------------------------------------------
    #
    # Measured on a real label, whole-phrase matching located every PASSING
    # rule and not one FAILURE. Failures quote badly by their nature, so
    # these three cases are the feature working or not working.

    def test_locates_a_number_the_model_quoted_wrongly(self):
        """`FSSAI_LICENCE` quoted a 13-digit number because thirteen digits is
        WHY it failed. The quotation is not on the label and never will be —
        but the number it refers to is."""
        words = (word('Lic', 10, 10, width=20, height=10),
                 word('No.', 35, 10, width=20, height=10),
                 word('10015064000541', 60, 10, width=120, height=10))

        self.assertTrue(ocr_locate('Lic No. 1001506400541', words, (300, 100)))

    def test_locates_a_quotation_spanning_separate_parts_of_the_label(self):
        """`EXPIRY_DATE` quoted "USE BY: (U) REFER TO BODY" — two places on the
        artwork, with the second set vertically alongside. No contiguous run
        of words contains it, but "USE BY" locates the right one."""
        words = (word('USE', 10, 10, width=25, height=10),
                 word('BY:', 40, 10, width=20, height=10),
                 word('REFER', 10, 80, width=40, height=10, line=(2, 1, 1)))

        self.assertTrue(
            ocr_locate('USE BY: (U) REFER TO BODY', words, (300, 100)))

    def test_locates_a_legible_table_row(self):
        """`NUTRITION_PANEL` quotes a row of the nutrition panel. Read
        properly — which needs `service.RENDER_DPI`, see there — the row
        anchors precisely."""
        words = (word('Energy', 10, 10, width=50, height=10),
                 word('(kcal)', 65, 10, width=40, height=10),
                 word('900', 110, 10, width=30, height=10))

        self.assertTrue(
            ocr_locate('Energy (kcal) 900 122.85', words, (300, 100)))

    def test_gives_up_when_the_reading_is_too_garbled_to_anchor(self):
        """Precision over recall, deliberately.

        At 300 dpi Tesseract could not read this label's row headings at all,
        leaving "kcal" in the FOOTNOTE as the only anchor for a nutrition
        finding — which drew a box on the wrong part of the page. No box is
        the right answer when nothing matches closely enough; the fix for the
        missing highlight is resolution, not a lower bar.
        """
        words = (word('mcg', 10, 80, width=30, height=10, line=(9, 1, 1)),
                 word('kcal)', 45, 80, width=35, height=10, line=(9, 1, 1)))

        self.assertEqual(
            ocr_locate('Energy (kcal) 900 122.85', words, (300, 100)), [])

    def test_does_not_fall_back_to_an_indistinctive_word(self):
        """A ladder that matched "of" or "no" would highlight half the label,
        which is worse than highlighting nothing."""
        words = (word('Manufactured', 10, 10, width=90, height=10),
                 word('by', 105, 10, width=15, height=10))

        self.assertEqual(
            ocr_locate('by no of', words, (300, 100)), [])

    def test_does_not_anchor_a_long_quotation_on_one_stray_word(self):
        """The bug this guards against was visible on a real label: the
        quotation "Images are for illustrative purposes only" was set in grey
        on maroon and never read, so the ladder fell to the single word
        "Image" and boxed it in the artwork's internal-use footer — a
        confident mark in the wrong place, which a reviewer cannot tell is
        wrong."""
        words = (word('Image:', 10, 90, width=45, height=10, line=(9, 1, 1)),
                 word('YES', 60, 90, width=30, height=10, line=(9, 1, 1)))

        self.assertEqual(
            ocr_locate('Images are for illustrative purposes only',
                       words, (300, 100)),
            [])

    def test_prefers_the_longest_confirmed_sub_phrase(self):
        """Among candidates, more of the quotation matched is better evidence
        of place than a shorter perfect match elsewhere."""
        words = (word('Best', 10, 10, width=40, height=10),
                 word('Before', 55, 10, width=50, height=10),
                 word('Before', 10, 80, width=50, height=10, line=(2, 1, 1)))

        [box] = ocr_locate('Best Before 12 months', words, (300, 100))

        # The pair on line 1, not the lone "Before" on line 2.
        self.assertLess(box['y'], 0.5)


class WordConfidenceTests(TestCase):
    """Low-confidence words keep their boxes.

    This is a corrected mistake, not a preference. The box list was filtered
    at confidence 30, and on a real label that discarded the FSSAI licence
    number (confidence 10) and MRP (confidence 9) — both read CORRECTLY.
    Tesseract's confidence tracks how small and stylised the print is, and
    statutory declarations ARE the small stylised print, so the filter removed
    exactly the declarations a reviewer most needs pointed at.
    """

    def _fake_tesseract(self, rows):
        """A stand-in for the pytesseract module, returning `rows`."""
        data = {key: [] for key in (
            'text', 'conf', 'left', 'top', 'width', 'height',
            'block_num', 'par_num', 'line_num')}
        for text, conf in rows:
            data['text'].append(text)
            data['conf'].append(conf)
            for key, value in (('left', 10), ('top', 10), ('width', 40),
                               ('height', 10), ('block_num', 1),
                               ('par_num', 1), ('line_num', 1)):
                data[key].append(value)

        class FakeImage:
            """Only `.size` is read by `ocr.read`."""
            size = (300, 100)

        class FakeOutput:
            DICT = 'dict'

        class FakeModule:
            Output = FakeOutput

            @staticmethod
            def image_to_data(image, output_type=None):
                return data

        return FakeModule, FakeImage()

    def test_a_low_confidence_word_still_gets_a_box(self):
        fake, image = self._fake_tesseract([('10015064000541', 10), ('MRP', 9)])

        with patch.object(service.ocr, '_tesseract', return_value=fake),              patch.dict('sys.modules', {'pytesseract': fake}):
            result = service.ocr.read(image)

        self.assertEqual(len(result.words), 2)
        self.assertIn('10015064000541', result.text)

    def test_structural_rows_are_not_words(self):
        """Tesseract emits -1 rows for page/block structure, not text."""
        fake, image = self._fake_tesseract([('', -1), ('MRP', 9)])

        with patch.object(service.ocr, '_tesseract', return_value=fake),              patch.dict('sys.modules', {'pytesseract': fake}):
            result = service.ocr.read(image)

        self.assertEqual([w.text for w in result.words], ['MRP'])


class AttachRegionsTests(TestCase):
    """Which finding gets pointed at what."""

    WORDS = (
        word('FSSAI', 20, 10, width=50, height=10, line=(1, 1, 1)),
        word('Best', 20, 60, width=40, height=10, line=(2, 1, 1)),
        word('Before', 65, 60, width=50, height=10, line=(2, 1, 1)),
    )

    def _finding(self, code='R1', status='PASS', evidence=''):
        return RuleFinding(rule_id=code, rule_name='Rule', status=status,
                           remarks='.', evidence_text=evidence)

    def _read(self):
        return fake_read('FSSAI Best Before', self.WORDS, (200, 100))

    def test_uses_the_quotation_the_model_gave(self):
        findings = [self._finding(evidence='Best Before')]

        service.attach_regions(findings, self._read(), [])

        self.assertEqual(len(findings[0].regions), 1)

    def test_falls_back_to_the_rules_own_wording(self):
        """A finding whose quotation cannot be located can still point at the
        declaration the rule is about."""
        rule = ComplianceRule(code='R1', name='R', rule_text='.',
                              critical_tokens=['FSSAI'])
        findings = [self._finding(evidence='something never printed')]

        service.attach_regions(findings, self._read(), [rule])

        self.assertEqual(len(findings[0].regions), 1)

    def test_an_absent_declaration_gets_no_region(self):
        findings = [self._finding(status='FAIL', evidence='')]

        service.attach_regions(findings, self._read(), [])

        self.assertEqual(findings[0].regions, [])

    def test_nothing_is_attached_when_ocr_was_unavailable(self):
        """No Tesseract means no boxes — and no guessing from the image."""
        findings = [self._finding(evidence='Best Before')]

        service.attach_regions(findings, None, [])

        self.assertEqual(findings[0].regions, [])


class SummaryTests(TestCase):
    def test_counts_and_compliance(self):
        findings = [
            RuleFinding(rule_id='A', rule_name='A', status='PASS', remarks='.'),
            RuleFinding(rule_id='B', rule_name='B', status='FAIL', remarks='.'),
        ]

        self.assertEqual(service.summarise(findings), {
            'total': 2, 'passed': 1, 'failed': 1, 'compliant': False})

    def test_all_passing_is_compliant(self):
        findings = [
            RuleFinding(rule_id='A', rule_name='A', status='PASS', remarks='.')]

        self.assertTrue(service.summarise(findings)['compliant'])

    def test_no_findings_is_not_compliant(self):
        """Vacuous truth is the wrong answer here: a label nothing was checked
        against has not been shown to comply with anything."""
        self.assertFalse(service.summarise([])['compliant'])


@override_settings(GEMINI_API_KEY='test-key')
class LabelCheckViewTests(TestCase):
    """The endpoint, end to end, with only the model call mocked."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username='zz-legal-api', password='pw', name='Legal API')
        ComplianceRule.objects.create(
            code='FSSAI_LICENCE', name='FSSAI logo and licence number',
            rule_text='A 14-digit FSSAI licence number must be present.',
            critical_tokens=['FSSAI'], is_critical=True, sort_order=10)
        ComplianceRule.objects.create(
            code='MRP_DECLARATION', name='MRP inclusive of all taxes',
            rule_text='MRP must be followed by (Incl. of all taxes).',
            sort_order=20)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _post(self, reply, ocr_text='FSSAI 10012345678901  MRP Rs 250'):
        with patch.object(service, 'call_gemini', return_value=reply) as call, \
             patch.object(service.ocr, 'extract_text', return_value=ocr_text):
            response = self.client.post(
                UPLOAD_URL, {'label_file': png_upload()}, format='multipart')
        return response, call

    def test_returns_the_structure_the_frontend_maps(self):
        reply = gemini_reply(
            ('FSSAI_LICENCE', 'FSSAI logo and licence number', 'PASS',
             'Licence 10012345678901 printed below the logo.'),
            ('MRP_DECLARATION', 'MRP inclusive of all taxes', 'FAIL',
             'MRP is printed without the inclusive-of-taxes wording.'),
        )

        response, _ = self._post(reply)

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['rule_count'], 2)
        self.assertTrue(body['ocr_available'])

        # The rendered preview, not the upload — a PDF in an <img> shows
        # nothing, and that is the common upload.
        self.assertIn('previews/', body['image_url'])
        self.assertTrue(body['image_url'].endswith('.png'))

        self.assertEqual(body['summary'],
                         {'total': 2, 'passed': 1, 'failed': 1,
                          'compliant': False})

        # Every finding carries exactly the five keys the checklist renders.
        for finding in body['findings']:
            self.assertEqual(
                set(finding),
                {'rule_id', 'rule_name', 'status', 'remarks', 'evidence_text',
                 'ocr_verified', 'regions'})
            self.assertIn(finding['status'], ('PASS', 'FAIL'))
            self.assertTrue(finding['remarks'].strip())

        first = body['findings'][0]
        self.assertEqual(first['rule_id'], 'FSSAI_LICENCE')
        self.assertTrue(first['ocr_verified'])

    def test_the_report_is_stored_with_the_upload(self):
        """A check that cannot be looked at afterwards cannot be defended."""
        reply = gemini_reply(
            ('FSSAI_LICENCE', 'FSSAI logo and licence number', 'PASS', 'Present.'),
            ('MRP_DECLARATION', 'MRP inclusive of all taxes', 'PASS', 'Present.'),
        )

        self._post(reply)

        row = LabelData.objects.latest('id')
        self.assertIn('FSSAI', row.ocr_text)
        self.assertEqual(len(row.report_json['findings']), 2)
        self.assertTrue(row.report_json['summary']['compliant'])

    def test_the_prompt_carries_every_active_rule(self):
        """The rules are only 'dynamic' if the row text actually reaches the
        model. Asserting on the prompt is the only place that is visible."""
        reply = gemini_reply(
            ('FSSAI_LICENCE', 'FSSAI logo and licence number', 'PASS', '.'),
            ('MRP_DECLARATION', 'MRP inclusive of all taxes', 'PASS', '.'),
        )

        _, call = self._post(reply)

        prompt = call.call_args.args[1]
        self.assertIn('A 14-digit FSSAI licence number must be present.', prompt)
        self.assertIn('MRP must be followed by (Incl. of all taxes).', prompt)
        self.assertIn('FSSAI 10012345678901', prompt)  # the OCR text

    def test_an_inactive_rule_is_not_checked(self):
        ComplianceRule.objects.filter(code='MRP_DECLARATION').update(is_active=False)
        reply = gemini_reply(
            ('FSSAI_LICENCE', 'FSSAI logo and licence number', 'PASS', '.'))

        response, call = self._post(reply)

        self.assertEqual(response.json()['rule_count'], 1)
        self.assertNotIn('Incl. of all taxes', call.call_args.args[1])

    def test_it_still_reports_when_tesseract_is_missing(self):
        """A missing native binary costs the cross-reference, not the feature.
        `ocr_available` is how the UI says which kind of answer this is."""
        reply = gemini_reply(
            ('FSSAI_LICENCE', 'FSSAI logo and licence number', 'PASS', '.'),
            ('MRP_DECLARATION', 'MRP inclusive of all taxes', 'PASS', '.'),
        )

        with patch.object(service, 'call_gemini', return_value=reply), \
             patch.object(service.ocr, 'extract_text',
                          side_effect=service.ocr.OcrUnavailable('no binary')):
            response = self.client.post(
                UPLOAD_URL, {'label_file': png_upload()}, format='multipart')

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertFalse(body['ocr_available'])
        self.assertEqual(body['summary']['total'], 2)
        # Nothing was overturned on the strength of text nobody read.
        self.assertTrue(all(f['ocr_verified'] is None for f in body['findings']))

    def test_no_rules_configured_is_a_message_not_a_crash(self):
        ComplianceRule.objects.all().update(is_active=False)

        with patch.object(service.ocr, 'read', return_value=fake_read()):
            response = self.client.post(
                UPLOAD_URL, {'label_file': png_upload()}, format='multipart')

        self.assertEqual(response.status_code, 400)
        self.assertIn('No compliance rules', response.json()['message'])

    def test_a_model_failure_becomes_a_readable_message(self):
        with patch.object(service, 'call_gemini',
                          side_effect=service.LabelCheckError('quota exhausted')), \
             patch.object(service.ocr, 'read', return_value=fake_read()):
            response = self.client.post(
                UPLOAD_URL, {'label_file': png_upload()}, format='multipart')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['message'], 'quota exhausted')

    def test_missing_file_is_rejected(self):
        response = self.client.post(UPLOAD_URL, {}, format='multipart')

        self.assertEqual(response.status_code, 400)
        self.assertIn('No file', response.json()['message'])


class ComplianceRuleApiTests(TestCase):
    """The rule book is editable without a deploy — that is the whole point."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username='zz-rules-api', password='pw', name='Rules API')

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_create_and_list_in_sort_order(self):
        self.client.post(RULES_URL, {
            'code': 'B_RULE', 'name': 'Second', 'rule_text': 'Second rule.',
            'sort_order': 20}, format='json')
        self.client.post(RULES_URL, {
            'code': 'A_RULE', 'name': 'First', 'rule_text': 'First rule.',
            'sort_order': 10}, format='json')

        body = self.client.get(RULES_URL).json()

        self.assertEqual([r['code'] for r in body], ['A_RULE', 'B_RULE'])

    def test_code_is_immutable_once_created(self):
        """Issued reports cite the code as rule_id; changing it orphans them."""
        created = self.client.post(RULES_URL, {
            'code': 'KEEP_ME', 'name': 'Rule', 'rule_text': 'Text.'},
            format='json').json()

        self.client.patch(f"{RULES_URL}{created['id']}/",
                          {'code': 'RENAMED', 'name': 'Renamed'}, format='json')

        rule = ComplianceRule.objects.get(id=created['id'])
        self.assertEqual(rule.code, 'KEEP_ME')
        self.assertEqual(rule.name, 'Renamed')

    def test_critical_tokens_must_be_a_list_of_strings(self):
        response = self.client.post(RULES_URL, {
            'code': 'BAD_TOKENS', 'name': 'Rule', 'rule_text': 'Text.',
            'critical_tokens': {'not': 'a list'}}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('critical_tokens', response.json())


class LabelCheckHistoryTests(TestCase):
    """Past checks stay reachable.

    A compliance report is a record of what was reviewed and when. One that
    can only be seen once is not a record — which is what it was: every check
    was stored, and nothing could open it.

    The properties worth pinning:

    * The list carries the SUMMARY but not the findings. A page of 25 full
      reports is a megabyte of JSON to render a list of filenames.
    * Reopening returns the report AS IT WAS — including the highlight boxes
      computed by whatever version of the locator ran at the time. A record
      shows what was reported, not what today's code would report.
    * Checks from the previous pipeline are not listed. They have no report to
      open, so a row would be a link to a blank page.
    """

    HISTORY_URL = '/api/legal/history/'

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username='zz-history', password='pw', name='Priya')
        cls.item = LabelItem.objects.create(item_name='Mustard Oil 200ml')

        cls.passed = LabelData.objects.create(
            label_file='labels/passed.pdf',
            preview_image='/media/labels/previews/passed.png',
            checked_by=cls.user, label_item=cls.item,
            ocr_text='FSSAI 10015064000541',
            report_json={
                'findings': [{
                    'rule_id': 'FSSAI_LICENCE', 'rule_name': 'FSSAI licence',
                    'status': 'PASS', 'remarks': 'Present.',
                    'evidence_text': 'Lic No. 10015064000541',
                    'ocr_verified': True,
                    'regions': [{'x': 0.1, 'y': 0.2, 'width': 0.3,
                                 'height': 0.05}],
                }],
                'summary': {'total': 1, 'passed': 1, 'failed': 0,
                            'compliant': True},
                'ocr_available': True,
                'rule_count': 1,
            })
        cls.failed = LabelData.objects.create(
            label_file='labels/failed.pdf',
            report_json={
                'findings': [{
                    'rule_id': 'BARCODE_PRESENT', 'rule_name': 'Barcode',
                    'status': 'FAIL', 'remarks': 'No barcode.',
                    'evidence_text': '', 'ocr_verified': None, 'regions': [],
                }],
                'summary': {'total': 1, 'passed': 0, 'failed': 1,
                            'compliant': False},
                'ocr_available': False,
                'rule_count': 1,
            })
        # A row from the previous pipeline: no report to open.
        cls.legacy = LabelData.objects.create(
            label_file='labels/legacy.pdf',
            parameter_json={'food_name': {'value': 'Old shape'}})

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_lists_checks_newest_first(self):
        body = self.client.get(self.HISTORY_URL).json()

        results = body['data']['results']
        self.assertEqual([r['id'] for r in results],
                         [self.failed.id, self.passed.id])
        self.assertEqual(body['data']['pagination']['total'], 2)

    def test_the_list_carries_the_summary_but_not_the_findings(self):
        [first, *_] = self.client.get(self.HISTORY_URL).json()['data']['results']

        self.assertEqual(first['summary']['failed'], 1)
        self.assertNotIn('findings', first)

    def test_the_list_says_who_ran_the_check_and_against_what(self):
        results = self.client.get(self.HISTORY_URL).json()['data']['results']
        row = next(r for r in results if r['id'] == self.passed.id)

        self.assertEqual(row['checked_by_name'], 'Priya')
        self.assertEqual(row['item_name'], 'Mustard Oil 200ml')
        self.assertEqual(row['file_name'], 'passed.pdf')
        self.assertEqual(row['image_url'], '/media/labels/previews/passed.png')

    def test_a_check_with_no_report_is_not_listed(self):
        """The previous pipeline's rows would be a link to a blank page."""
        ids = [r['id'] for r in
               self.client.get(self.HISTORY_URL).json()['data']['results']]

        self.assertNotIn(self.legacy.id, ids)

    def test_filters_to_failures(self):
        body = self.client.get(self.HISTORY_URL, {'failed_only': '1'}).json()

        self.assertEqual([r['id'] for r in body['data']['results']],
                         [self.failed.id])

    def test_filters_by_item(self):
        body = self.client.get(self.HISTORY_URL, {'item': self.item.id}).json()

        self.assertEqual([r['id'] for r in body['data']['results']],
                         [self.passed.id])

    def test_reopening_returns_the_report_as_it_was(self):
        body = self.client.get(f'{self.HISTORY_URL}{self.passed.id}/').json()

        self.assertEqual(body['rule_count'], 1)
        self.assertTrue(body['ocr_available'])
        [finding] = body['findings']
        self.assertEqual(finding['rule_id'], 'FSSAI_LICENCE')
        # The stored boxes, so the reopened report highlights exactly what the
        # reviewer saw — even though the locator has changed since.
        self.assertEqual(finding['regions'],
                         [{'x': 0.1, 'y': 0.2, 'width': 0.3, 'height': 0.05}])

    def test_an_unattributed_old_row_reports_a_blank_author(self):
        """Rows predating attribution say nothing rather than guess."""
        body = self.client.get(f'{self.HISTORY_URL}{self.failed.id}/').json()

        self.assertEqual(body['checked_by_name'], '')
        self.assertEqual(body['item_name'], '')

    def test_a_pdf_upload_is_never_offered_as_an_image(self):
        """The bug that broke every historic thumbnail.

        `preview_image` arrived after these checks ran, so it is blank on all
        of them, and the fallback was the uploaded file — a PDF for almost
        every label. A PDF in an `<img>` is a broken image, not a fallback.
        """
        row = LabelData.objects.create(
            label_file='labels/no-preview.pdf',
            report_json={'findings': [], 'summary': {}},
        )

        body = self.client.get(f'{self.HISTORY_URL}{row.id}/').json()

        self.assertEqual(body['image_url'], '')

    def test_an_image_upload_is_still_offered_directly(self):
        """A PNG upload needs no preview to be displayable."""
        row = LabelData.objects.create(
            label_file='labels/direct.png',
            report_json={'findings': [], 'summary': {}},
        )

        body = self.client.get(f'{self.HISTORY_URL}{row.id}/').json()

        self.assertTrue(body['image_url'].endswith('direct.png'))

    def test_an_unrecorded_preview_is_recovered_from_disk(self):
        """The previews were always written; only the path went unrecorded.
        Finding the file is better than telling the user it is gone."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        row = LabelData.objects.create(
            label_file='labels/recovered.pdf',
            report_json={'findings': [], 'summary': {}},
        )
        saved = default_storage.save('labels/previews/recovered.png',
                                     ContentFile(png_bytes()))
        try:
            body = self.client.get(f'{self.HISTORY_URL}{row.id}/').json()
            self.assertTrue(body['image_url'].endswith('recovered.png'))
        finally:
            default_storage.delete(saved)

    def test_history_is_behind_the_legal_gate(self):
        outsider = User.objects.create_user(
            username='zz-outsider-history', password='pw', name='Outsider')
        client = APIClient()
        client.force_authenticate(outsider)

        self.assertEqual(client.get(self.HISTORY_URL).status_code, 403)
        self.assertEqual(
            client.get(f'{self.HISTORY_URL}{self.passed.id}/').status_code, 403)
