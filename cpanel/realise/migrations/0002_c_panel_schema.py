"""Move the Control Panel's tables into their own schema, `c_panel`.

They were created by 0001 wherever the connection's search_path put new tables
first (OMS's `payments` schema). Each is moved with ALTER TABLE ... SET SCHEMA —
rows, indexes, constraints and its id sequence go with it — and given C_Panel's
own table name (`realise_<model>`). The model state follows with
AlterModelTable; see cpanel/core/db.py.
"""
from django.db import migrations

from cpanel.core.db import c_panel_table


class Migration(migrations.Migration):

    dependencies = [
        ('cp_realise', '0001_initial'),
    ]

    operations = [
        # The schema the Control Panel's tables live in (cpanel/core/db.py).
        migrations.RunSQL('CREATE SCHEMA IF NOT EXISTS c_panel', reverse_sql=migrations.RunSQL.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('monthlytarget', c_panel_table('realise_monthlytarget'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_monthlytarget" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_monthlytarget" RENAME TO "realise_monthlytarget";',
                reverse_sql='ALTER TABLE c_panel."realise_monthlytarget" RENAME TO "cp_realise_monthlytarget"; ALTER TABLE c_panel."cp_realise_monthlytarget" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('maingroupmaster', c_panel_table('realise_maingroupmaster'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_maingroupmaster" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_maingroupmaster" RENAME TO "realise_maingroupmaster";',
                reverse_sql='ALTER TABLE c_panel."realise_maingroupmaster" RENAME TO "cp_realise_maingroupmaster"; ALTER TABLE c_panel."cp_realise_maingroupmaster" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('statemaster', c_panel_table('realise_statemaster'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_statemaster" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_statemaster" RENAME TO "realise_statemaster";',
                reverse_sql='ALTER TABLE c_panel."realise_statemaster" RENAME TO "cp_realise_statemaster"; ALTER TABLE c_panel."cp_realise_statemaster" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('targetmaster', c_panel_table('realise_targetmaster'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_targetmaster" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_targetmaster" RENAME TO "realise_targetmaster";',
                reverse_sql='ALTER TABLE c_panel."realise_targetmaster" RENAME TO "cp_realise_targetmaster"; ALTER TABLE c_panel."cp_realise_targetmaster" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('segmenttarget', c_panel_table('realise_segmenttarget'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_segmenttarget" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_segmenttarget" RENAME TO "realise_segmenttarget";',
                reverse_sql='ALTER TABLE c_panel."realise_segmenttarget" RENAME TO "cp_realise_segmenttarget"; ALTER TABLE c_panel."cp_realise_segmenttarget" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('territorymapping', c_panel_table('realise_territorymapping'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_territorymapping" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_territorymapping" RENAME TO "realise_territorymapping";',
                reverse_sql='ALTER TABLE c_panel."realise_territorymapping" RENAME TO "cp_realise_territorymapping"; ALTER TABLE c_panel."cp_realise_territorymapping" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('cityowner', c_panel_table('realise_cityowner'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_cityowner" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_cityowner" RENAME TO "realise_cityowner";',
                reverse_sql='ALTER TABLE c_panel."realise_cityowner" RENAME TO "cp_realise_cityowner"; ALTER TABLE c_panel."cp_realise_cityowner" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('territoryproducttarget', c_panel_table('realise_territoryproducttarget'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_territoryproducttarget" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_territoryproducttarget" RENAME TO "realise_territoryproducttarget";',
                reverse_sql='ALTER TABLE c_panel."realise_territoryproducttarget" RENAME TO "cp_realise_territoryproducttarget"; ALTER TABLE c_panel."cp_realise_territoryproducttarget" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('territoryitemtarget', c_panel_table('realise_territoryitemtarget'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_territoryitemtarget" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_territoryitemtarget" RENAME TO "realise_territoryitemtarget";',
                reverse_sql='ALTER TABLE c_panel."realise_territoryitemtarget" RENAME TO "cp_realise_territoryitemtarget"; ALTER TABLE c_panel."cp_realise_territoryitemtarget" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('targetnode', c_panel_table('realise_targetnode'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_targetnode" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_targetnode" RENAME TO "realise_targetnode";',
                reverse_sql='ALTER TABLE c_panel."realise_targetnode" RENAME TO "cp_realise_targetnode"; ALTER TABLE c_panel."cp_realise_targetnode" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('closingremark', c_panel_table('realise_closingremark'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_closingremark" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_closingremark" RENAME TO "realise_closingremark";',
                reverse_sql='ALTER TABLE c_panel."realise_closingremark" RENAME TO "cp_realise_closingremark"; ALTER TABLE c_panel."cp_realise_closingremark" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('creditlock', c_panel_table('realise_creditlock'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_creditlock" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_creditlock" RENAME TO "realise_creditlock";',
                reverse_sql='ALTER TABLE c_panel."realise_creditlock" RENAME TO "cp_realise_creditlock"; ALTER TABLE c_panel."cp_realise_creditlock" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('creditlocksnapshot', c_panel_table('realise_creditlocksnapshot'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_creditlocksnapshot" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_creditlocksnapshot" RENAME TO "realise_creditlocksnapshot";',
                reverse_sql='ALTER TABLE c_panel."realise_creditlocksnapshot" RENAME TO "cp_realise_creditlocksnapshot"; ALTER TABLE c_panel."cp_realise_creditlocksnapshot" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('agingremark', c_panel_table('realise_agingremark'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_agingremark" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_agingremark" RENAME TO "realise_agingremark";',
                reverse_sql='ALTER TABLE c_panel."realise_agingremark" RENAME TO "cp_realise_agingremark"; ALTER TABLE c_panel."cp_realise_agingremark" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('agingremarkline', c_panel_table('realise_agingremarkline'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_agingremarkline" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_agingremarkline" RENAME TO "realise_agingremarkline";',
                reverse_sql='ALTER TABLE c_panel."realise_agingremarkline" RENAME TO "cp_realise_agingremarkline"; ALTER TABLE c_panel."cp_realise_agingremarkline" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('claim', c_panel_table('realise_claim'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_claim" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_claim" RENAME TO "realise_claim";',
                reverse_sql='ALTER TABLE c_panel."realise_claim" RENAME TO "cp_realise_claim"; ALTER TABLE c_panel."cp_realise_claim" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('agingdueconfig', c_panel_table('realise_agingdueconfig'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_agingdueconfig" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_agingdueconfig" RENAME TO "realise_agingdueconfig";',
                reverse_sql='ALTER TABLE c_panel."realise_agingdueconfig" RENAME TO "cp_realise_agingdueconfig"; ALTER TABLE c_panel."cp_realise_agingdueconfig" SET SCHEMA public;',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('ratelist', c_panel_table('realise_ratelist'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_realise_ratelist" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_realise_ratelist" RENAME TO "realise_ratelist";',
                reverse_sql='ALTER TABLE c_panel."realise_ratelist" RENAME TO "cp_realise_ratelist"; ALTER TABLE c_panel."cp_realise_ratelist" SET SCHEMA public;',
            )],
        ),
        # Index names derive from the table name; rename them in their new schema.
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('agingremark', new_name='realise_agi_card_co_3ef4e1_idx', old_name='cp_realise__card_co_594808_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__card_co_594808_idx" RENAME TO "realise_agi_card_co_3ef4e1_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_agi_card_co_3ef4e1_idx" RENAME TO "cp_realise__card_co_594808_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('agingremarkline', new_name='realise_agi_card_co_ce4e57_idx', old_name='cp_realise__card_co_33a2a1_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__card_co_33a2a1_idx" RENAME TO "realise_agi_card_co_ce4e57_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_agi_card_co_ce4e57_idx" RENAME TO "cp_realise__card_co_33a2a1_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('cityowner', new_name='realise_cit_channel_46a8c9_idx', old_name='cp_realise__channel_b01719_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__channel_b01719_idx" RENAME TO "realise_cit_channel_46a8c9_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_cit_channel_46a8c9_idx" RENAME TO "cp_realise__channel_b01719_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('claim', new_name='realise_cla_claim_d_9a8b87_idx', old_name='cp_realise__claim_d_f5fc2d_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__claim_d_f5fc2d_idx" RENAME TO "realise_cla_claim_d_9a8b87_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_cla_claim_d_9a8b87_idx" RENAME TO "cp_realise__claim_d_f5fc2d_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('claim', new_name='realise_cla_party_c_c7d7c4_idx', old_name='cp_realise__party_c_e3d105_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__party_c_e3d105_idx" RENAME TO "realise_cla_party_c_c7d7c4_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_cla_party_c_c7d7c4_idx" RENAME TO "cp_realise__party_c_e3d105_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('creditlocksnapshot', new_name='realise_cre_lock_id_c1c4ce_idx', old_name='cp_realise__lock_id_4cdccc_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__lock_id_4cdccc_idx" RENAME TO "realise_cre_lock_id_c1c4ce_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_cre_lock_id_c1c4ce_idx" RENAME TO "cp_realise__lock_id_4cdccc_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('monthlytarget', new_name='realise_mon_year_76f6cd_idx', old_name='cp_realise__year_147fa9_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__year_147fa9_idx" RENAME TO "realise_mon_year_76f6cd_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_mon_year_76f6cd_idx" RENAME TO "cp_realise__year_147fa9_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('monthlytarget', new_name='realise_mon_product_a815aa_idx', old_name='cp_realise__product_bd98f7_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__product_bd98f7_idx" RENAME TO "realise_mon_product_a815aa_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_mon_product_a815aa_idx" RENAME TO "cp_realise__product_bd98f7_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('segmenttarget', new_name='realise_seg_segment_e01115_idx', old_name='cp_realise__segment_1178fe_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__segment_1178fe_idx" RENAME TO "realise_seg_segment_e01115_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_seg_segment_e01115_idx" RENAME TO "cp_realise__segment_1178fe_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('targetmaster', new_name='realise_tar_year_edbed4_idx', old_name='cp_realise__year_c5de87_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__year_c5de87_idx" RENAME TO "realise_tar_year_edbed4_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_tar_year_edbed4_idx" RENAME TO "cp_realise__year_c5de87_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('targetmaster', new_name='realise_tar_main_gr_ad7d59_idx', old_name='cp_realise__main_gr_8fe1e2_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__main_gr_8fe1e2_idx" RENAME TO "realise_tar_main_gr_ad7d59_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_tar_main_gr_ad7d59_idx" RENAME TO "cp_realise__main_gr_8fe1e2_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('targetnode', new_name='realise_tar_year_2a271a_idx', old_name='cp_realise__year_07bfb4_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__year_07bfb4_idx" RENAME TO "realise_tar_year_2a271a_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_tar_year_2a271a_idx" RENAME TO "cp_realise__year_07bfb4_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('territoryitemtarget', new_name='realise_ter_year_29eb31_idx', old_name='cp_realise__year_f22532_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__year_f22532_idx" RENAME TO "realise_ter_year_29eb31_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_ter_year_29eb31_idx" RENAME TO "cp_realise__year_f22532_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('territoryitemtarget', new_name='realise_ter_channel_28a481_idx', old_name='cp_realise__channel_88caf3_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__channel_88caf3_idx" RENAME TO "realise_ter_channel_28a481_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_ter_channel_28a481_idx" RENAME TO "cp_realise__channel_88caf3_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('territoryitemtarget', new_name='realise_ter_item_co_829905_idx', old_name='cp_realise__item_co_807925_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__item_co_807925_idx" RENAME TO "realise_ter_item_co_829905_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_ter_item_co_829905_idx" RENAME TO "cp_realise__item_co_807925_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('territorymapping', new_name='realise_ter_channel_9d56aa_idx', old_name='cp_realise__channel_1fd3a6_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__channel_1fd3a6_idx" RENAME TO "realise_ter_channel_9d56aa_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_ter_channel_9d56aa_idx" RENAME TO "cp_realise__channel_1fd3a6_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('territoryproducttarget', new_name='realise_ter_year_7a5459_idx', old_name='cp_realise__year_652bbe_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__year_652bbe_idx" RENAME TO "realise_ter_year_7a5459_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_ter_year_7a5459_idx" RENAME TO "cp_realise__year_652bbe_idx"',
            )],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RenameIndex('territoryproducttarget', new_name='realise_ter_channel_a83a47_idx', old_name='cp_realise__channel_ca0fb2_idx')],
            database_operations=[migrations.RunSQL(
                'ALTER INDEX c_panel."cp_realise__channel_ca0fb2_idx" RENAME TO "realise_ter_channel_a83a47_idx"',
                reverse_sql='ALTER INDEX c_panel."realise_ter_channel_a83a47_idx" RENAME TO "cp_realise__channel_ca0fb2_idx"',
            )],
        ),
    ]
