"""`/api/orders/branch/` after the Branch consolidation (plan item 3.4).

The endpoint used to read `orders.Branches`, a second unmanaged model on the
`branches` table that declared every column wrongly. It now reads
`sap_sync.Branch`, the model the SAP sync writes and the one that matches the
table.

Nothing here touches the database. `branches` is unmanaged, so it does not
exist in the SQLite test database at all — and the view's `.distinct('bpl_name')`
is Postgres-only DISTINCT ON, which SQLite cannot execute. What is asserted is
the part that changed: which model is read, and the shape of the JSON.
"""
from django.test import TestCase

from orders.serializers import BranchSerializer
from sap_sync.models import Branch


class BranchSerializerTests(TestCase):

    def test_bpl_id_is_still_serialised_as_a_string(self):
        """The regression this exists for.

        `orders.Branches.bpl_id` was a CharField over an `integer` column, so
        this endpoint has always emitted "5". `sap_sync.Branch` declares the
        column correctly as an IntegerField, so switching models would have
        silently changed the JSON to 5 for every existing client.

        OMS-Frontend wraps this value in String() at all nine of its use sites
        and would not have noticed. The React Native client is a separate
        repository, not in this workspace, and cannot be checked — so the wire
        format is preserved rather than corrected. Changing it is a deliberate
        API change for later, not a side effect of a refactor.
        """
        data = BranchSerializer(Branch(bpl_id=5, bpl_name='X FACTORY',
                                       category='OIL')).data
        self.assertEqual(data['bpl_id'], '5')
        self.assertIsInstance(data['bpl_id'], str)

    def test_the_response_keeps_its_three_fields(self):
        """`sap_sync.BranchSerializer` shares this name and exposes seven
        fields. Reading the other one would add is_active/created_at/updated_at
        and an `id` to a response that has never carried them."""
        data = BranchSerializer(Branch(bpl_id=5, bpl_name='X FACTORY',
                                       category='OIL')).data
        self.assertEqual(set(data), {'bpl_id', 'bpl_name', 'category'})

    def test_the_serializer_is_bound_to_the_sap_sync_model(self):
        self.assertIs(BranchSerializer.Meta.model, Branch)


class BranchViewTests(TestCase):

    def test_the_view_reads_the_sap_sync_model(self):
        """Guards against `orders.Branches` being reintroduced — the duplicate
        was invisible precisely because both models worked."""
        from orders import views

        self.assertIs(views.Branch, Branch)
        self.assertFalse(hasattr(views, 'Branches'),
                         'the duplicate model is back in orders.views')

    def test_the_duplicate_model_is_gone_from_orders(self):
        from django.apps import apps
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises((LookupError, ImproperlyConfigured)):
            apps.get_model('orders', 'Branches')
