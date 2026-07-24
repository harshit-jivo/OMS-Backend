"""Drop the release-policy table.

Manual release management is gone: the deployed app is now the sole source of
truth for its own version and build, so nothing needs a server-side record of
what "should" be running.

`0001_initial` is already applied in production and also creates
`devices_user_device`, so it is left untouched and the table is removed
forwards. This drops `devices_app_release` and its constraints/indexes
(`apprelease_build_uq`, `apprelease_one_latest_uq`, `apprelease_lookup_idx`)
— Django emits those as part of the DROP TABLE.

`UserDevice` has no foreign key, relation or import dependency on `AppRelease`
— the two were only ever compared by value in application code — so device
tracking is untouched by this migration.

Irreversible on purpose: reversing it would recreate an empty table that no
code reads, which would be misleading rather than useful. To roll the feature
back, restore the model and generate a fresh migration.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="AppRelease"),
    ]
