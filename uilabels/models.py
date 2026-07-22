from django.db import models


class UILabel(models.Model):
    """A single dynamic UI field label, editable by admins from the dashboard.

    Both web and mobile clients fetch these once after login and render
    `display_name` wherever the corresponding `field_key` appears, so wording
    can change org-wide without a frontend/mobile code change or redeploy.

    The table is intentionally generic (one row per field key) so more labels
    can be added later — `price_list`, `basic_price`, `tax`, `confirm_button`,
    … — without any backend change beyond a data row.
    """

    field_key = models.CharField(
        max_length=100,
        unique=True,
        help_text="Stable machine key used by clients, e.g. 'price_list'. "
                  "Never edited after creation.",
    )
    display_name = models.CharField(
        max_length=100,
        help_text="The text shown to end users, e.g. 'Distributor Price'.",
    )
    description = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'uilabels_uilabel'
        ordering = ['field_key']
        verbose_name = 'UI Label'
        verbose_name_plural = 'UI Labels'

    def __str__(self):
        return f'{self.field_key} → {self.display_name}'
