"""Merge the two `users` leaves.

`0026_partyproductassignment_combo_free_item` (combo free-item fields) and
`0028_merge_0026_hais_role_0027_payment_roles` (role branches) were developed in
parallel off `0025_user_categories` and touch different models, so joining them
needs no operations.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0026_partyproductassignment_combo_free_item'),
        ('users', '0028_merge_0026_hais_role_0027_payment_roles'),
    ]

    operations = [
    ]
