"""Clean the BKDT runtime model down to three tables.

    backdate_request          -> backdate
    backdate_approval_action  -> backdate_action_logs
    backdate_approval_task    -> dropped entirely

WHY THE TASK TABLE GOES
-----------------------
Every task row held a stage id, a sequence and a status. The stage that
matters — the one being waited on — is now a column on the flow; the stages
already passed are in the action log; the stages still ahead are configuration
the engine already stores. The table restated all three and had to be kept in
step with them.

WHY THIS IS A RENAME AND NOT A REBUILD
--------------------------------------
`RenameModel` / `RenameField` keep the rows. This module is new and its data is
small, but a rebuild would still throw away real requests people raised, and
nothing here needs the physical column order a rebuild would buy: the LOGICAL
order — models, serializers, admin, API — is what readers see, and that is
declared in `models.py`.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No JSAP importer. `backdate.userDocument`, `backdate.jsFlow` and
`backdate.jsFlowStatus` stay in JSAP; OMS BKDT starts fresh.

DATA CARRIED ACROSS
-------------------
* `flow.current_stage` / `current_user` are back-filled from the open task of
  each pending flow, so requests already waiting keep waiting in the right
  place instead of being stranded.
* `flow.total_stage` is back-filled from the stage count of its workflow.
* `hana_status` collapses to SUCCESS / FAILED / NULL: `APPLIED` -> `SUCCESS`,
  and `NA` / `PENDING` -> NULL, which is the same claim ("SAP has not been
  written to") stated once instead of twice.
* Log rows keep their action, actor, stage, remarks and timestamp, and are
  re-pointed from the flow to the request. `SUBMIT` becomes `CREATE`. The
  `HANA_APPLIED` / `HANA_FAILED` rows are DELETED: the SAP outcome lives on the
  flow, and a second copy in an append-only log is how the two come to
  disagree.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _link_logs(apps, schema_editor):
    """Point every log row at its REQUEST, and tidy the vocabulary."""
    ActionLog = apps.get_model('backdate', 'BackDateActionLog')
    WorkflowStage = apps.get_model('workflow', 'WorkflowStage')

    # The SAP outcome is the flow's, not a person's act.
    ActionLog.objects.filter(
        action__in=['HANA_APPLIED', 'HANA_FAILED']).delete()

    live_stages = set(WorkflowStage.objects.values_list('pk', flat=True))
    for row in ActionLog.objects.select_related('flow').all():
        row.backdate_id = row.flow.backdate_id
        row.action = 'CREATE' if row.action == 'SUBMIT' else row.action
        # A stage id that no longer resolves cannot become a foreign key.
        row.stage_id = (row.legacy_stage_id
                        if row.legacy_stage_id in live_stages else None)
        row.save(update_fields=['backdate', 'action', 'stage'])


def _fill_flow(apps, schema_editor):
    """Back-fill the flow's new columns from the task table, before it goes."""
    BackDateFlow = apps.get_model('backdate', 'BackDateFlow')
    Task = apps.get_model('backdate', 'BackDateApprovalTask')
    WorkflowStage = apps.get_model('workflow', 'WorkflowStage')

    for flow in BackDateFlow.objects.all():
        open_task = (Task.objects
                     .filter(flow_id=flow.pk, status='PENDING')
                     .order_by('sequence').first())
        stage_id = open_task.stage_id if open_task else None
        # The stage may have been deleted since; a dangling id would break the
        # new foreign key.
        stage = (WorkflowStage.objects.filter(pk=stage_id).first()
                 if stage_id else None)

        flow.current_stage_id = stage.pk if stage else None
        flow.current_user_id = stage.user_id if stage else None
        flow.total_stage = WorkflowStage.objects.filter(
            workflow_id=flow.workflow_id, is_active=True).count()

        if flow.hana_status == 'APPLIED':
            flow.hana_status = 'SUCCESS'
        elif flow.hana_status in ('NA', 'PENDING', ''):
            flow.hana_status = None

        flow.save(update_fields=['current_stage', 'current_user',
                                 'total_stage', 'hana_status'])


def _unfill_flow(apps, schema_editor):
    """Enough of a reverse that the migration can be re-applied."""
    BackDateFlow = apps.get_model('backdate', 'BackDateFlow')
    for flow in BackDateFlow.objects.all():
        if flow.hana_status == 'SUCCESS':
            flow.hana_status = 'APPLIED'
        elif flow.hana_status is None:
            flow.hana_status = 'NA'
        flow.save(update_fields=['hana_status'])


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0003_action_may_be_both'),
        ('workflow', '0007_drop_runtime_and_testflow'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── 1. constraints/indexes named for the old table come off first ─
        migrations.RemoveConstraint(
            model_name='backdaterequest',
            name='backdate_request_company_valid'),
        migrations.RemoveConstraint(
            model_name='backdaterequest',
            name='backdate_request_dates_ordered'),
        migrations.RemoveConstraint(
            model_name='backdaterequest',
            name='backdate_request_action_valid'),
        migrations.RemoveIndex(
            model_name='backdaterequest', name='bkdt_req_creator_idx'),
        migrations.RemoveIndex(
            model_name='backdaterequest', name='bkdt_req_company_idx'),
        migrations.RemoveConstraint(
            model_name='backdateapprovalaction', name='bkdt_action_flow_seq_uq'),
        migrations.RemoveConstraint(
            model_name='backdateapprovaltask', name='bkdt_task_flow_seq_uq'),
        migrations.RemoveIndex(
            model_name='backdateflow', name='bkdt_flow_status_idx'),

        # ── 2. backdate_request -> backdate ──────────────────────────────
        migrations.RenameModel(
            old_name='BackDateRequest', new_name='BackDate'),
        # `AlterModelTable` emits `RENAME TO "backdate"."backdate"`, and
        # Postgres takes an UNQUALIFIED name there — the `schema"."table`
        # trick that puts these tables in their own schema cannot express the
        # rename. So the rename is written by hand and the model state is told
        # about it separately.
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(
                'ALTER TABLE backdate.backdate_request RENAME TO backdate;',
                'ALTER TABLE backdate.backdate RENAME TO backdate_request;',
            )],
            state_operations=[migrations.AlterModelTable(
                name='backdate', table='backdate"."backdate')],
        ),
        migrations.AlterModelOptions(
            name='backdate',
            options={'ordering': ['-created_at'],
                     'verbose_name': 'BackDate',
                     'verbose_name_plural': 'BackDate requests'},
        ),

        # ── 3. flow.request -> flow.backdate ─────────────────────────────
        migrations.RenameField(
            model_name='backdateflow', old_name='request',
            new_name='backdate'),
        migrations.AlterField(
            model_name='backdateflow', name='backdate',
            field=models.OneToOneField(
                db_column='backdate_id',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='flow', to='backdate.backdate'),
        ),

        # ── 4. flow gains its runtime state ──────────────────────────────
        migrations.AddField(
            model_name='backdateflow', name='current_stage',
            field=models.ForeignKey(
                blank=True, db_column='current_stage', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+', to='workflow.workflowstage'),
        ),
        migrations.AddField(
            model_name='backdateflow', name='current_user',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='backdateflow', name='total_stage',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name='backdateflow', name='hana_status',
            field=models.CharField(
                blank=True, choices=[('SUCCESS', 'Success'),
                                     ('FAILED', 'Failed')],
                max_length=10, null=True),
        ),
        # Back-filled from the task table while it still exists.
        migrations.RunPython(_fill_flow, _unfill_flow),

        migrations.RemoveField(model_name='backdateflow',
                               name='current_sequence'),
        migrations.RemoveField(model_name='backdateflow',
                               name='hana_applied_at'),
        migrations.RemoveField(model_name='backdateflow',
                               name='matched_query'),

        # ── 5. action -> action log ──────────────────────────────────────
        migrations.RenameModel(
            old_name='BackDateApprovalAction', new_name='BackDateActionLog'),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(
                'ALTER TABLE backdate.backdate_approval_action '
                'RENAME TO backdate_action_logs;',
                'ALTER TABLE backdate.backdate_action_logs '
                'RENAME TO backdate_approval_action;',
            )],
            state_operations=[migrations.AlterModelTable(
                name='backdateactionlog',
                table='backdate"."backdate_action_logs')],
        ),
        # The old plain-integer stage column steps aside so the FK can take
        # its name, keeping the values through the copy below.
        migrations.RenameField(
            model_name='backdateactionlog', old_name='stage_id',
            new_name='legacy_stage_id'),
        migrations.AddField(
            model_name='backdateactionlog', name='stage',
            field=models.ForeignKey(
                blank=True, db_column='stage_id', null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+', to='workflow.workflowstage'),
        ),
        migrations.AddField(
            model_name='backdateactionlog', name='backdate',
            field=models.ForeignKey(
                db_column='backdate_id', null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='action_logs', to='backdate.backdate'),
        ),
        migrations.AddField(
            model_name='backdateactionlog', name='action_data',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(_link_logs, migrations.RunPython.noop),

        migrations.RemoveField(model_name='backdateactionlog',
                               name='legacy_stage_id'),
        migrations.RemoveField(model_name='backdateactionlog', name='flow'),
        migrations.RemoveField(model_name='backdateactionlog',
                               name='sequence'),
        migrations.RemoveField(model_name='backdateactionlog',
                               name='stage_name'),
        migrations.RemoveField(model_name='backdateactionlog',
                               name='on_behalf_of'),
        migrations.AlterField(
            model_name='backdateactionlog', name='backdate',
            field=models.ForeignKey(
                db_column='backdate_id',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='action_logs', to='backdate.backdate'),
        ),
        migrations.AlterField(
            model_name='backdateactionlog', name='action',
            field=models.CharField(
                choices=[('CREATE', 'Created'), ('UPDATE', 'Updated'),
                         ('APPROVE', 'Approved'), ('REJECT', 'Rejected')],
                max_length=10),
        ),
        migrations.AlterField(
            model_name='backdateactionlog', name='acted_by',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='backdate_action_logs',
                to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterModelOptions(
            name='backdateactionlog',
            options={'ordering': ['acted_at', 'id'],
                     'verbose_name': 'BackDate Action Log'},
        ),

        # ── 6. the task table goes ───────────────────────────────────────
        migrations.RemoveIndex(
            model_name='backdateapprovaltask', name='bkdt_task_open_idx'),
        migrations.DeleteModel(name='BackDateApprovalTask'),

        # ── 7. constraints and indexes, renamed to match ─────────────────
        migrations.AddConstraint(
            model_name='backdate',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ('company__in', ['OIL', 'BEVERAGES', 'MART'])),
                name='backdate_company_valid'),
        ),
        migrations.AddConstraint(
            model_name='backdate',
            constraint=models.CheckConstraint(
                condition=models.Q(('to_date__gte', models.F('from_date'))),
                name='backdate_dates_ordered'),
        ),
        migrations.AddConstraint(
            model_name='backdate',
            constraint=models.CheckConstraint(
                condition=models.Q(('action__in', ['A', 'U', 'A,U'])),
                name='backdate_action_valid'),
        ),
        migrations.AddIndex(
            model_name='backdate',
            index=models.Index(fields=['created_by', '-created_at'],
                               name='bkdt_creator_idx'),
        ),
        migrations.AddIndex(
            model_name='backdate',
            index=models.Index(fields=['company'], name='bkdt_company_idx'),
        ),
        migrations.AddIndex(
            model_name='backdateflow',
            index=models.Index(fields=['status', 'current_user'],
                               name='bkdt_flow_queue_idx'),
        ),
        migrations.AddIndex(
            model_name='backdateflow',
            index=models.Index(fields=['current_stage'],
                               name='bkdt_flow_stage_idx'),
        ),
        migrations.AddIndex(
            model_name='backdateflow',
            index=models.Index(fields=['workflow'],
                               name='bkdt_flow_workflow_idx'),
        ),
        migrations.AddIndex(
            model_name='backdateactionlog',
            index=models.Index(fields=['backdate', 'acted_at'],
                               name='bkdt_log_backdate_idx'),
        ),
    ]
