"""Collapse company scope to one `company` column; drop query `type`/`key_column`.

TWO TABLES, ONE DESIGN
----------------------
`workflow_queries` AND `workflows` both change. Simplifying only the query
table would leave `workflows.company_scope='ALL' / company=NULL` sitting beside
`workflow_queries.company='ALL'` — two contradictory ways of saying the same
thing, which is exactly what the brief forbids. Both now store `ALL`, `OIL`,
`BEVERAGES` or `MART` directly, and `CompanyScope` is gone.

WHAT ELSE GOES, AND WHY IT IS SAFE
----------------------------------
* `workflow_queries.type` — JSAP parity only; nothing in the engine ever read
  it. Audited before writing this: 0 rows hold a non-empty value.
* `workflow_queries.key_column` — asked an administrator to declare which
  column identifies a document. That is runtime context the business module
  owns and now passes to `engine.start(..., key_column=...)`. Audited: 0 rows
  hold a non-default value, so no configured behaviour is lost.

ORDER MATTERS
-------------
The data step runs AFTER the old CHECKs are dropped and BEFORE `company`
becomes NOT NULL. It cannot run earlier: writing `company='ALL'` onto a row
whose `company_scope='ALL'` violates `*_company_scope_consistent`, which
requires `company IS NULL` in that case. It must not run later either: by then
`company_scope` is gone and the mapping is unrecoverable.

It is written explicitly rather than left to `AlterField`'s implicit NULL-fill
so the mapping is stated, and so an impossible legacy row stops the migration
instead of being silently coerced.
"""
from django.db import migrations, models


def _carry_scope_into_company(apps, schema_editor):
    """`(scope, company)` -> `company`.

        ALL      + NULL  ->  'ALL'
        SPECIFIC + 'OIL' ->  'OIL'

    Anything else is an impossible combination the old CHECKs were supposed to
    prevent. If one exists the data is not what this migration assumes, so it
    raises rather than guessing — a wrong guess here silently re-routes live
    approvals.
    """
    valid = {'ALL', 'OIL', 'BEVERAGES', 'MART'}
    for label in ('Workflow', 'WorkflowQuery'):
        model = apps.get_model('workflow', label)
        broken = []
        for row in model.objects.all():
            if row.company_scope == 'ALL':
                if row.company:
                    broken.append(f'{label}#{row.pk}: scope=ALL but '
                                  f'company={row.company!r}')
                    continue
                row.company = 'ALL'
            elif row.company_scope == 'SPECIFIC':
                if row.company not in valid - {'ALL'}:
                    broken.append(f'{label}#{row.pk}: scope=SPECIFIC but '
                                  f'company={row.company!r}')
                    continue
                # Already the value it should keep; written back for clarity.
                row.company = row.company
            else:
                broken.append(f'{label}#{row.pk}: unknown '
                              f'company_scope={row.company_scope!r}')
                continue
            row.save(update_fields=['company'])

        if broken:
            raise RuntimeError(
                'Cannot migrate workflow company scope — these rows hold a '
                'combination the new single-column design has no meaning for. '
                'Fix them and re-run:\n  ' + '\n  '.join(broken))


def _split_company_into_scope(apps, schema_editor):
    """Reverse: `company` -> `(scope, company)`."""
    for label in ('Workflow', 'WorkflowQuery'):
        model = apps.get_model('workflow', label)
        for row in model.objects.all():
            if row.company == 'ALL':
                row.company_scope, row.company = 'ALL', None
            else:
                row.company_scope = 'SPECIFIC'
            row.save(update_fields=['company_scope', 'company'])


class Migration(migrations.Migration):

    dependencies = [
        ('workflow', '0005_simplify_module_registry'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='workflow',
            name='workflow_company_scope_valid',
        ),
        migrations.RemoveConstraint(
            model_name='workflow',
            name='workflow_company_code_valid',
        ),
        migrations.RemoveConstraint(
            model_name='workflow',
            name='workflow_company_scope_consistent',
        ),
        migrations.RemoveConstraint(
            model_name='workflowquery',
            name='workflow_query_company_scope_valid',
        ),
        migrations.RemoveConstraint(
            model_name='workflowquery',
            name='workflow_query_company_code_valid',
        ),
        migrations.RemoveConstraint(
            model_name='workflowquery',
            name='workflow_query_company_scope_consistent',
        ),
        migrations.RemoveIndex(
            model_name='workflow',
            name='workflow_module_scope_idx',
        ),
        migrations.RemoveIndex(
            model_name='workflowquery',
            name='workflow_query_scope_idx',
        ),
        # Old CHECKs are gone by here, and `company_scope` still exists —
        # the only window in which the mapping can be written.
        migrations.RunPython(_carry_scope_into_company,
                             _split_company_into_scope),
        migrations.RemoveField(
            model_name='workflow',
            name='company_scope',
        ),
        migrations.RemoveField(
            model_name='workflowquery',
            name='company_scope',
        ),
        migrations.RemoveField(
            model_name='workflowquery',
            name='key_column',
        ),
        migrations.RemoveField(
            model_name='workflowquery',
            name='type',
        ),
        migrations.AlterField(
            model_name='workflow',
            name='company',
            field=models.CharField(choices=[('ALL', 'All companies'), ('OIL', 'Oil'), ('BEVERAGES', 'Beverages'), ('MART', 'Mart')], default='ALL', help_text="'ALL' applies to every company; otherwise the one company code this row applies to.", max_length=20),
        ),
        migrations.AlterField(
            model_name='workflowquery',
            name='company',
            field=models.CharField(choices=[('ALL', 'All companies'), ('OIL', 'Oil'), ('BEVERAGES', 'Beverages'), ('MART', 'Mart')], default='ALL', help_text="'ALL' applies to every company; otherwise the one company code this row applies to.", max_length=20),
        ),
        migrations.AddIndex(
            model_name='workflow',
            index=models.Index(fields=['module', 'company'], name='workflow_module_scope_idx'),
        ),
        migrations.AddIndex(
            model_name='workflowquery',
            index=models.Index(fields=['workflow', 'company'], name='workflow_query_scope_idx'),
        ),
        migrations.AddConstraint(
            model_name='workflow',
            constraint=models.CheckConstraint(condition=models.Q(('company__in', ['ALL', 'OIL', 'BEVERAGES', 'MART'])), name='workflow_company_valid'),
        ),
        migrations.AddConstraint(
            model_name='workflowquery',
            constraint=models.CheckConstraint(condition=models.Q(('company__in', ['ALL', 'OIL', 'BEVERAGES', 'MART'])), name='workflow_query_company_valid'),
        ),
    ]
