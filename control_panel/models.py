"""What the Control Panel stores: the targets and budgets SAP does not hold.

Ported from C_Panel's SQLite database (`realise` and `dashboard` apps) with the
same fields and the same uniqueness rules, so the screens behave identically.
Tables are prefixed `control_panel_` in `public`, as advance_payment's are.

Nothing here is migrated from C_Panel's data: OMS starts with empty targets
and budgets, entered on the Realise and Expenses screens.
"""
from django.conf import settings
from django.db import models


class MonthlyTarget(models.Model):
    """Litres and rate target per product type and sub-group for one month —
    the grid the Realise screen's KPI cards are measured against."""

    PRODUCT_TYPES = [('PREMIUM', 'Premium'), ('COMMODITY', 'Commodity')]

    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPES)
    sub_group = models.CharField(max_length=100)
    month = models.IntegerField()
    year = models.IntegerField()
    tgt_ltrs = models.FloatField(default=0)
    tgt_rate = models.FloatField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'control_panel_monthly_target'
        unique_together = ('product_type', 'sub_group', 'month', 'year')
        ordering = ['-year', '-month', 'product_type', 'sub_group']
        indexes = [
            models.Index(fields=['year', 'month'], name='cp_mtarget_period_idx'),
            models.Index(fields=['product_type', 'sub_group'], name='cp_mtarget_group_idx'),
        ]

    @property
    def key(self):
        return f'{self.product_type}|{self.sub_group}'

    def __str__(self):
        return f'{self.key} {self.month}/{self.year}'


class MainGroupMaster(models.Model):
    """The sales channels (GT, MT, E-COM, ...) targets are set against."""

    name = models.CharField(max_length=50, unique=True)

    class Meta:
        db_table = 'control_panel_main_group'
        ordering = ['name']

    def __str__(self):
        return self.name


class StateMaster(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = 'control_panel_state'
        ordering = ['name']

    def __str__(self):
        return self.name


class TargetMaster(models.Model):
    """Channel x state x sales-person litre target (the older, fixed-depth
    target table; `TargetNode` is the free-form one that replaced it on the
    screen, kept because the channel-target editor still writes both)."""

    main_group = models.ForeignKey(MainGroupMaster, on_delete=models.CASCADE)
    state = models.ForeignKey(StateMaster, null=True, blank=True, on_delete=models.SET_NULL)
    sales_person = models.CharField(max_length=100, null=True, blank=True)
    target_ltrs = models.DecimalField(max_digits=12, decimal_places=2)
    month = models.IntegerField()
    year = models.IntegerField()

    class Meta:
        db_table = 'control_panel_target_master'
        unique_together = ('main_group', 'state', 'sales_person', 'month', 'year')
        ordering = ['-year', '-month', 'main_group__name', 'state__name', 'sales_person']
        indexes = [
            models.Index(fields=['year', 'month'], name='cp_tmaster_period_idx'),
            models.Index(fields=['main_group'], name='cp_tmaster_group_idx'),
        ]

    def __str__(self):
        state = self.state.name if self.state_id else 'ALL'
        return f'{self.main_group.name} {state} {self.sales_person or "ALL"} {self.month}/{self.year}'


class SegmentTarget(models.Model):
    """Flat per-value target for a single dimension (main group, state, person
    or item)."""

    SEGMENT_TYPES = [
        ('main_group', 'Main Group'),
        ('state', 'State'),
        ('person', 'Person'),
        ('premium_item', 'Premium Items'),
        ('commodity_item', 'Commodity Items'),
    ]

    segment_type = models.CharField(max_length=20, choices=SEGMENT_TYPES)
    segment_value = models.CharField(max_length=100)
    month = models.IntegerField()
    year = models.IntegerField()
    target_ltrs = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    target_realise_value = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'control_panel_segment_target'
        unique_together = ('segment_type', 'segment_value', 'month', 'year')
        ordering = ['segment_type', 'segment_value']
        indexes = [
            models.Index(fields=['segment_type', 'year', 'month'], name='cp_starget_period_idx'),
        ]

    def __str__(self):
        return f'{self.segment_type}:{self.segment_value} {self.month}/{self.year}'


class TargetNode(models.Model):
    """Free-form hierarchical target. Any of the three dimensions may be blank,
    so a target can be held at any level (GT only, GT+Punjab, GT+Punjab+a
    person). Blank means "that dimension is not part of this node". Nothing is
    split automatically between levels."""

    main_group = models.CharField(max_length=50, blank=True, default='')
    state = models.CharField(max_length=100, blank=True, default='')
    sales_person = models.CharField(max_length=100, blank=True, default='')
    #: '' = all segments, else PREMIUM / COMMODITY.
    segment = models.CharField(max_length=20, blank=True, default='')
    month = models.IntegerField()
    year = models.IntegerField()
    target_ltrs = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    target_realise = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'control_panel_target_node'
        unique_together = ('main_group', 'state', 'sales_person', 'segment', 'month', 'year')
        ordering = ['main_group', 'state', 'sales_person']
        indexes = [
            models.Index(fields=['year', 'month'], name='cp_tnode_period_idx'),
        ]

    def __str__(self):
        combo = '+'.join(p for p in (self.main_group, self.state, self.sales_person) if p) or 'ALL'
        return f'{combo} {self.month}/{self.year}'


class ExpenseBudget(models.Model):
    """The monthly budget per expense head, entered on the Expenses screen."""

    budget_head = models.CharField(max_length=100)
    month = models.IntegerField()
    year = models.IntegerField()
    budget_amount = models.FloatField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')

    class Meta:
        db_table = 'control_panel_expense_budget'
        unique_together = ('budget_head', 'month', 'year')
        ordering = ['budget_head']

    def __str__(self):
        return f'{self.budget_head} {self.month}/{self.year}'
