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
    # Field-behaviour flags. These only apply when the row represents an actual
    # input field (e.g. 'po_number') rather than a pure text label (e.g.
    # 'price_list'). `is_enabled` controls whether clients show the field at all;
    # `is_required` controls whether it is mandatory when shown. Both are
    # admin-editable so a field can be turned on/off or made optional/required
    # without a frontend or mobile code change.
    is_enabled = models.BooleanField(
        default=True,
        help_text="Field-only: clients hide the field when this is off.",
    )
    is_required = models.BooleanField(
        default=False,
        help_text="Field-only: clients make the field mandatory when this is on.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'uilabels_uilabel'
        ordering = ['field_key']
        verbose_name = 'UI Label'
        verbose_name_plural = 'UI Labels'

    def __str__(self):
        return f'{self.field_key} → {self.display_name}'
