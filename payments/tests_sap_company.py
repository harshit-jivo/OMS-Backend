"""Where a payment posts comes from the environment, not a database row.

The company database used to live in `payments.SapCompanyMap`, which meant
moving TEST to LIVE was an UPDATE nobody reviewed, and the value could differ
silently between environments. These tests pin the replacement: settings decide
the target, the canonical resolver is the only path to it, and an unknown
company is refused rather than quietly treated as OIL.
"""
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from serviceLayer.service import SAPServiceLayerManager

from .sap_company import default_bpl_id, resolve_company_db


@override_settings(HANA_OIL_COMPANY_DB='UNIT_OIL',
                   HANA_BEVERAGE_COMPANY_DB='UNIT_BEV',
                   HANA_MART_COMPANY_DB='UNIT_MART')
class CompanyDatabaseTests(SimpleTestCase):
    """Each company reaches its own SAP database — and only its own."""

    def test_each_company_resolves_to_its_configured_database(self):
        self.assertEqual(resolve_company_db('OIL'), 'UNIT_OIL')
        self.assertEqual(resolve_company_db('BEVERAGES'), 'UNIT_BEV')
        self.assertEqual(resolve_company_db('MART'), 'UNIT_MART')

    def test_no_two_companies_share_a_target(self):
        """A Mart payment landing in the Oil company is the failure to fear."""
        targets = {c: resolve_company_db(c)
                   for c in ('OIL', 'BEVERAGES', 'MART')}
        self.assertEqual(len(set(targets.values())), 3, targets)

    def test_it_delegates_to_the_canonical_resolver(self):
        """One resolver, so a payment and its invoice cannot disagree."""
        for company in ('OIL', 'BEVERAGES', 'MART'):
            self.assertEqual(resolve_company_db(company),
                             SAPServiceLayerManager.schema_for(company))

    def test_case_and_whitespace_are_tolerated(self):
        self.assertEqual(resolve_company_db('  oil  '), 'UNIT_OIL')

    def test_an_unknown_company_is_refused_not_defaulted(self):
        """`schema_for` falls back to OIL for anything it does not recognise.

        Right for a sales document, wrong for money: a typo would post to the
        wrong company's ledger. So this layer refuses first.
        """
        for bad in ('BOGUS', '', None, 'OILS'):
            with self.assertRaises(ValidationError):
                resolve_company_db(bad)

    def test_changing_the_setting_changes_the_target(self):
        """The whole point: TEST to LIVE is a deployment change."""
        with override_settings(HANA_OIL_COMPANY_DB='LIVE_OIL'):
            self.assertEqual(resolve_company_db('OIL'), 'LIVE_OIL')

    @override_settings(HANA_OIL_COMPANY_DB='')
    def test_an_unconfigured_company_is_refused(self):
        """Better to refuse than to post into an empty schema name."""
        with self.assertRaises(ValidationError):
            resolve_company_db('OIL')


class DefaultBranchTests(SimpleTestCase):
    """The fallback branch, for the two cases with nothing to inherit from."""

    @override_settings(HANA_OIL_DEFAULT_BPL_ID=1,
                       HANA_BEVERAGE_DEFAULT_BPL_ID=2,
                       HANA_MART_DEFAULT_BPL_ID=3)
    def test_each_company_has_its_own_default(self):
        """The branch lists genuinely differ per company in SAP."""
        self.assertEqual(default_bpl_id('OIL'), 1)
        self.assertEqual(default_bpl_id('BEVERAGES'), 2)
        self.assertEqual(default_bpl_id('MART'), 3)

    @override_settings(HANA_OIL_DEFAULT_BPL_ID=7)
    def test_it_returns_an_int_not_a_string(self):
        """It becomes payload['BPLID']; SAP will not take a string."""
        self.assertIsInstance(default_bpl_id('OIL'), int)

    def test_an_unknown_company_is_none_not_an_exception(self):
        """Callers already handle 'no branch'; each decides if that is fatal."""
        self.assertIsNone(default_bpl_id('BOGUS'))

    @override_settings(HANA_OIL_DEFAULT_BPL_ID='')
    def test_a_blank_setting_is_none(self):
        self.assertIsNone(default_bpl_id('OIL'))

    @override_settings(HANA_OIL_DEFAULT_BPL_ID='not-a-number')
    def test_a_malformed_setting_is_none_not_a_crash(self):
        """A bad value must not take down every payment for that company."""
        self.assertIsNone(default_bpl_id('OIL'))


class NoHardcodedSchemaTests(SimpleTestCase):
    """No SAP database name may be written into payments business logic."""

    def test_payments_modules_name_no_sap_database(self):
        import ast
        import pathlib

        banned = ('JIVO_OIL_HANADB', 'JIVO_BEVERAGES_HANADB',
                  'JIVO_MART_HANADB', 'TEST_JIVO_OIL_HANADB',
                  'TEST_JIVO_BEVERAGES_HANADB', 'TEST_JIVO_MART_HANADB')
        root = pathlib.Path(__file__).resolve().parent
        offenders = []

        # Parsed, not grepped. Comments and docstrings legitimately cite real
        # databases as evidence — `sap_payloads` names the company a posting
        # was verified against, which is provenance worth keeping. What must
        # never appear is a database name in EXECUTABLE code, so only string
        # literals that are not docstrings are inspected.
        for path in sorted(root.glob('*.py')):
            if path.name.startswith('tests'):
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            docstrings = {
                node.body[0].value
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and node not in docstrings):
                    offenders += [
                        f'{path.name}:{node.lineno}: {name}'
                        for name in banned if name in node.value]

        self.assertEqual(offenders, [], offenders)
