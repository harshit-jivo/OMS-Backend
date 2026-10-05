from django.db import models

from cpanel.core.db import c_panel_table


class ExpenseBudget(models.Model):
    budget_head = models.CharField(max_length=100)
    month = models.IntegerField()
    year = models.IntegerField()
    budget_amount = models.FloatField(default=0)

    class Meta:
        db_table = c_panel_table('dashboard_expensebudget')
        unique_together = ('budget_head', 'month', 'year')
