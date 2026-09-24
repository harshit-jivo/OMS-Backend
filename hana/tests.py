"""HANA query builders — parameter binding and schema resolution.

`hana` had zero tests across 2,020 lines while holding every direct read of the
three SAP company databases, and its query builders formatted request values
straight into SQL. `get_customer_details` took
`request.query_params.get('card_code')` and dropped it into

    WHERE T0."CardCode" = '{party_code}'

on an endpoint that answered anonymous requests until this refactor.

Nothing here contacts HANA. The builders are pure functions returning
`(sql, params)`, so what they produce can be asserted directly — which is the
only way to test this without a live SAP connection.

Run with::

    python manage.py test hana --settings=OMS.test_settings
"""
from django.test import TestCase

from hana.services.connection import HanaSchemaError, Queries

#: (name, args) for every builder that takes a caller-supplied value.
#: `_placeholders_match_params` runs the whole table, so a new builder added
#: without binding its values shows up as soon as it is listed here.
VALUE_BUILDERS = [
    ('get_customer_details', ('C001', 'OIL')),
    ('get_warehouse_details', ('WH1', 'OIL')),
    ('get_salesperson_details', ('7', 'OIL')),
    ('get_addresse', ('C001', 'OIL')),
    ('get_state_chain', ('OIL', 'PB')),
    ('get_next_doc_no', ('17', 'OIL')),
    ('get_batch_details', ('FG1', 'WH1', 'OIL')),
    ('get_inventory_details', ('FG1', 'OIL')),
    ('get_item_price', ('FG1', 1, 'OIL')),
    ('get_costing_code', ('SUNFLOWER', 'OIL')),
    ('get_series', ('24', '1', 'OIL')),
    ('get_duplicate_num_at_card', ('PO-1', 'C001', 'OIL', 'ORDR')),
    ('get_draft_verification', ('REF1', 'OIL')),
    ('get_invoice_status', ('W', 'OIL')),
    ('get_docEntry', (1001, 'OIL')),
    ('get_order_doc_entry', (1001, 'OIL')),
    ('get_quotation_status', ([1, 2, 3], 'OIL')),
    ('get_sales_orders_for_party', ('C001', 'OIL')),
    ('get_sales_orders_for_product', ('FG1',)),
    ('get_fg_warehouse_stock', ('OIL', ['A', 'B'], 'WH1')),
    ('get_inventory_report', ('OIL', ['W1', 'W2'])),
]

#: A classic quote-break, a comment terminator, and a stacked statement.
INJECTION_PAYLOADS = [
    "' OR '1'='1",
    "x'; DROP TABLE OITM; --",
    "'--",
    "' UNION SELECT * FROM OCRD --",
]


class ParameterBindingTests(TestCase):

    def test_every_builder_returns_sql_and_params(self):
        for name, args in VALUE_BUILDERS:
            with self.subTest(builder=name):
                out = getattr(Queries, name)(*args)
                self.assertIsInstance(out, tuple, f'{name} still returns a bare string')
                self.assertEqual(len(out), 2)

    def test_placeholders_match_params(self):
        """The failure this catches is silent, not loud.

        hdbcli binds positionally, so a count mismatch is an error but a
        WRONG-ORDER list is not — it filters by the wrong column and returns
        the wrong rows. `get_fg_warehouse_stock` is the one at risk: its
        warehouse placeholder sits in the JOIN, above the WHERE clause, so its
        value has to bind before the item codes even though the item filter is
        built first.
        """
        for name, args in VALUE_BUILDERS:
            with self.subTest(builder=name):
                sql, params = getattr(Queries, name)(*args)
                self.assertEqual(
                    sql.count('?'), len(params),
                    f'{name}: {sql.count("?")} placeholders, {len(params)} params')

    def test_no_caller_value_is_interpolated(self):
        """Feed each builder an injection payload and assert it lands in the
        PARAMS, never in the SQL text."""
        for name, args in VALUE_BUILDERS:
            for payload in INJECTION_PAYLOADS:
                # Substitute the payload into each string argument in turn.
                for i, arg in enumerate(args):
                    if not isinstance(arg, str) or arg in {'OIL', 'ORDR'}:
                        continue
                    probe = list(args)
                    probe[i] = payload
                    with self.subTest(builder=name, arg=i, payload=payload):
                        try:
                            sql, params = getattr(Queries, name)(*probe)
                        except (HanaSchemaError, ValueError):
                            continue  # rejected outright, which is also correct
                        self.assertNotIn(
                            payload, sql,
                            f'{name} interpolated a caller value into the SQL')
                        # Compared case-insensitively: `get_duplicate_num_at_card`
                        # uppercases the reference before binding, because SAP
                        # stores NumAtCard uppercased. That is a matching rule,
                        # not escaping, so it is preserved.
                        self.assertIn(payload.upper(),
                                      [str(p).upper() for p in params])

    def test_a_list_argument_binds_one_placeholder_per_element(self):
        sql, params = Queries.get_quotation_status([11, 22, 33], 'OIL')
        self.assertEqual(sql.count('?'), 3)
        self.assertEqual(params, [11, 22, 33])

    def test_a_union_binds_its_value_once_per_branch(self):
        """`get_sales_orders_for_product` UNIONs across every configured company
        DB. A single bound value would fill only the first branch's
        placeholder, and hdbcli would reject the count — but the earlier
        string-formatted version had no such check."""
        sql, params = Queries.get_sales_orders_for_product('FG1')
        self.assertEqual(sql.count('?'), len(params))
        self.assertEqual(set(params), {'FG1'})
        self.assertEqual(len(params), len(Queries._open_so_schemas()))

    def test_product_sales_orders_accepts_the_branch_its_caller_passes(self):
        """`services.syncSalesOrderByProduct` has always passed `branch`, and
        the builder did not accept it — so every call raised TypeError and
        `GET /api/hana/product-so/` could never have worked.

        The argument is accepted and ignored: the query UNIONs across every
        company DB on purpose, because open sales orders for one item live in
        different databases by category.
        """
        with_branch = Queries.get_sales_orders_for_product('FG1', 'OIL')
        without = Queries.get_sales_orders_for_product('FG1')
        self.assertEqual(with_branch, without)

    def test_optional_filters_bind_nothing_when_absent(self):
        sql, params = Queries.get_fg_warehouse_stock('OIL')
        self.assertEqual(params, [])
        self.assertEqual(sql.count('?'), 0)

    def test_the_warehouse_filter_binds_before_the_item_filter(self):
        """Order, not just count — see `test_placeholders_match_params`."""
        sql, params = Queries.get_fg_warehouse_stock(
            'OIL', item_codes=['ITEM-A', 'ITEM-B'], whs_code='WH-9')
        self.assertEqual(params[0], 'WH-9',
                         'the JOIN placeholder must bind first')
        self.assertEqual(params[1:], ['ITEM-A', 'ITEM-B'])
        # And the JOIN placeholder really does precede the WHERE ones.
        self.assertLess(sql.index('AND T1."WhsCode" = ?'), sql.index('WHERE'))


class SchemaResolutionTests(TestCase):
    """Only a schema name may be interpolated, because HANA cannot bind an
    identifier — so it must come from settings, never a request."""

    def test_known_branches_resolve(self):
        self.assertEqual(Queries._schema_for_branch('OIL'), Queries.OIL_SCHEMA)
        self.assertEqual(Queries._schema_for_branch('BEVERAGE'),
                         Queries.BEVERAGE_SCHEMA)

    def test_resolution_is_case_and_whitespace_insensitive(self):
        self.assertEqual(Queries._schema_for_branch('  oil '), Queries.OIL_SCHEMA)

    def test_an_unknown_branch_raises_rather_than_defaulting(self):
        """Not a style point. The three company databases hold DIFFERENT
        documents under the same DocNum, so falling back to OIL does not fail —
        it silently returns another company's data.

        The old `if/elif` with no `else` left `s` unbound, so this surfaced as
        `UnboundLocalError` from inside a query builder.
        """
        for branch in ('', None, 'NOPE', 'oil; DROP TABLE OITM'):
            with self.subTest(branch=branch):
                with self.assertRaises(HanaSchemaError):
                    Queries._schema_for_branch(branch)

    def test_a_branch_name_cannot_smuggle_sql_into_the_schema_position(self):
        with self.assertRaises(HanaSchemaError):
            Queries.get_customer_details('C001', '"; DROP TABLE OITM; --')


class SellableBatchTests(TestCase):
    """A batch on hand is not the same as a batch that may be sold.

    SAP holds the answer in the batch master, OBTN."Status" -- 0 released,
    1 not accessible, 2 locked -- while OIBT reports only how much sits in
    the warehouse. Offering a locked batch to the FEFO picker produced an
    invoice SAP would not accept ("batch ... is locked or not accessible",
    10001133), and the refusal arrived at the very end, after the reviewer
    had approved and posted it.
    """

    def test_batch_details_asks_the_batch_master_for_the_status(self):
        sql, _params = Queries.get_batch_details('FG1', 'WH1', 'OIL')
        self.assertIn('OBTN', sql, 'the batch master is not consulted at all')
        self.assertIn('"Status" = \'0\'', sql,
                      'released is the only status whose stock may be sold')

    def test_the_join_is_on_the_batch_key_and_cannot_fan_out(self):
        """SysNumber is unique per item in OBTN; BatchNum is not a safe key.

        `160426` and `160426.` are two real, distinct batches of FG0000386.
        Matching on the printed number would conflate them, and a join that
        multiplies rows would hand the allocator the same stock twice.
        """
        sql, _params = Queries.get_batch_details('FG1', 'WH1', 'OIL')
        self.assertIn('T1."SysNumber" = T0."SysNumber"', sql)
        self.assertIn('T1."ItemCode" = T0."ItemCode"', sql)

    def test_the_status_filter_is_not_a_bindable_value(self):
        """Guards the params list: the builder still binds only item and
        warehouse, in that order. A third placeholder added here without a
        third value would bind the warehouse into the status column."""
        sql, params = Queries.get_batch_details('FG1', 'WH1', 'OIL')
        self.assertEqual(params, ['FG1', 'WH1'])
        self.assertEqual(sql.count('?'), 2)


class ExecuteContractTests(TestCase):
    """`execute` accepts the `(sql, params)` pair, which is what let the
    builders convert without touching all 29 callers in `services.py`."""

    class _FakeCursor:
        description = None

        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

    class _FakeConn:
        def commit(self):
            pass

        def rollback(self):
            pass

    def _conn(self):
        from hana.services.connection import HANAConnection

        conn = HANAConnection()
        conn.cursor = self._FakeCursor()
        conn.connection = self._FakeConn()
        return conn

    def test_a_pair_is_unpacked(self):
        conn = self._conn()
        conn.execute(('SELECT ?', ['x']))
        self.assertEqual(conn.cursor.calls, [('SELECT ?', ['x'])])

    def test_a_plain_string_still_works(self):
        conn = self._conn()
        conn.execute('SELECT 1')
        self.assertEqual(conn.cursor.calls, [('SELECT 1', [])])

    def test_passing_params_twice_is_refused(self):
        """Silently preferring one source would bind the wrong values."""
        conn = self._conn()
        with self.assertRaises(TypeError):
            conn.execute(('SELECT ?', ['a']), ['b'])


class GroupSalesOrdersTests(TestCase):
    """`/api/hana/so/` lines carry U_SchemeAgst for the invoice payload.

    SAP rejects an A/R invoice line whose U_SchemeAgst is blank, so the query
    resolves it (order line value, else item sub-group) and the grouper must
    hand it to the frontend under the name the payload uses.
    """

    def _row(self, **overrides):
        row = {
            'DocEntry': 10985, 'DocNum': 1, 'DocDate': '2026-09-21', 'DocDueDate': '2026-09-21',
            'CardCode': 'C001', 'CardName': 'Sabri', 'NumAtCard': '', 'DocStatus': 'O',
            'DocTotal': 0, 'VatSum': 0, 'DiscSum': 0, 'Comments': None,
            'SlpCode': 20, 'ShipToCode': 'S', 'PayToCode': 'B', 'BPLId': 2,
            'LineNum': 0, 'ItemCode': 'FG0000324', 'Dscription': 'Water', 'Quantity': 1800,
            'OpenQty': 1800, 'Price': 0, 'PriceBefDi': 0, 'DiscPrcnt': 0, 'LineTotal': 0,
            'VatPrcnt': 5, 'VatGroup': 'IGST@5', 'WhsCode': 'BH-FG', 'TaxCode': 'IGST@5',
            'ShipDate': '2026-09-21', 'AcctCode': None, 'Project': None, 'OcrCode': None,
            'LineStatus': 'O', 'SchemeAgst': 'WATER',
        }
        row.update(overrides)
        return row

    def test_scheme_against_is_passed_through_per_line(self):
        from hana.utils import group_sales_orders

        orders = group_sales_orders([self._row(), self._row(LineNum=1, SchemeAgst='DRINKS')])
        lines = orders[0]['lines']
        self.assertEqual([line['U_SchemeAgst'] for line in lines], ['WATER', 'DRINKS'])

    def test_missing_scheme_against_is_an_empty_string(self):
        """None would serialise as null; the frontend treats "" as "not set"."""
        from hana.utils import group_sales_orders

        orders = group_sales_orders([self._row(SchemeAgst=None)])
        self.assertEqual(orders[0]['lines'][0]['U_SchemeAgst'], '')

    def test_query_resolves_scheme_against_as_a_profit_centre_code(self):
        """Order line value, else its costing code, else OPRC code for the item's sub-group."""
        sql, _ = Queries.get_sales_orders_for_party('C001', 'OIL')
        self.assertIn("COALESCE(NULLIF(T1.\"U_SchemeAgst\", ''), NULLIF(T1.\"OcrCode\", ''), ", sql)
        self.assertIn(".\"OPRC\" AS P WHERE P.\"DimCode\" = 1 AND P.\"Active\" = 'Y'", sql)
        self.assertIn('(P."PrcCode" = T2."U_Sub_Group" OR P."PrcName" = T2."U_Sub_Group")', sql)
        self.assertIn('."OITM" AS T2', sql)
        self.assertIn('ON T2."ItemCode" = T1."ItemCode"', sql)

    def test_fg_items_carry_the_same_profit_centre_code(self):
        sql = Queries.get_fg_items('BEVERAGE')
        self.assertIn('(P."PrcCode" = T0."U_Sub_Group" OR P."PrcName" = T0."U_Sub_Group")', sql)
        self.assertIn('AS "SchemeAgst"', sql)
