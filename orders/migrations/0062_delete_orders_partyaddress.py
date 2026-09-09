"""Drop `orders.PartyAddress` — a duplicate master that never held a row.

`sap_sync.PartyAddress` (`sap_party_addresses`, 35,719 rows) is the live party
address master. `orders.PartyAddress` (`party_addresses`) declared a strict
subset of its columns, was never populated, and is read by nothing.

The name collision made this hard to see, so the check that matters is which
model each reference resolves to, not which name it spells:

    orders/serializers.py:5      from sap_sync.models import PartyAddress
                                     as SapPartyAddress
    orders/serializers.py:94     model = SapPartyAddress   <- the serializer
                                                              named
                                                              PartyAddressSerializer
    orders/views/masters.py:431  SapPartyAddress.objects.filter(...)
    orders/views/dashboards.py:96 SapPartyAddress.objects.filter(...)
    sap_sync/views.py:389        PartyAddress.objects.all()  (its own model)

Every consumer in the repository points at sap_sync. `orders.PartyAddress` has
no importer at all, no admin registration, no test, no raw SQL, and no foreign
key in either direction. The `/orders/addresses/` endpoint that looks like it
belongs to this model queries `SapPartyAddress`.

UNLIKE migration 0060, which removed a duplicate model that shared a table with
a live one, this model owns its own table and is `managed`. So this DOES emit
`DROP TABLE party_addresses` — verify with `sqlmigrate` before applying. The
table was exported first (0 rows) and the reverse migration recreates the
structure exactly, so the operation is reversible in both directions.

Its two sibling empty tables are deliberately NOT dropped here. `parties` is
read by `orders/views/dashboards.py:82` and by the scheme engine through
`apps.get_model('orders', 'Parties')`; `product_details` backs the live
`/products/` and `/product-filters/` endpoints. Both are empty, which is worth
investigating on its own, but empty is not the same as unused.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0061_order_items_order_id_index"),
    ]

    operations = [
        migrations.DeleteModel(name="PartyAddress"),
    ]
