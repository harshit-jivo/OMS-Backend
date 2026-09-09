"""Deliberately empty — Phase 4.1.

`hana` has no models. It holds query builders that read the three SAP company
databases directly (`hana/services/connection.py`), and it never owned a table
in `order_management`.

It does, however, have a row in `django_migrations` recording
`hana / 0001_initial` as applied — one of 44 orphans left by migration history
that has drifted from the tree. That row is not inert. With no file of this
name on disk, the day someone adds the first model to this app Django writes a
migration, names it `0001_initial`, finds the row already recorded, and
SILENTLY SKIPS IT. The model changes; the table never appears. Nothing fails
at the point of the mistake — it fails later, as a column that does not exist.

This file closes that off by taking the name. It performs no operations, so
applying it is a no-op on a database that has never seen it, and it is already
recorded as applied on the ones that have. The next migration here will be
`0002_...`, which is the point.

The row is left in place rather than deleted. Deleting it would work equally
well and destroys evidence of what happened for no gain; a file that documents
itself is worth more than a tidy table.

The other 43 orphan rows (`orders` 22, `users` 20, `sap_sync` 1) are genuinely
inert — their apps have migrations on disk and no name collides. They matter
only as a prerequisite for squashing (plan 4.6), which is not worth doing on a
61 MB database.
"""
from django.db import migrations


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = []
