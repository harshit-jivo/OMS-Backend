"""Where the Control Panel's tables live: their own PostgreSQL schema.

Every Control Panel model (the `cp_realise` and `cp_dashboard` apps) sets
`db_table = c_panel_table('<C_Panel's own table name>')`, so its table is
`c_panel.realise_targetnode`, `c_panel.dashboard_expensebudget`, ... — one
schema to browse, back up or grant, apart from OMS's `public` and `payments`.

The value is Django's quoted-identifier form (`c_panel"."name`): quote_name
wraps it into `"c_panel"."name"`, and index names are derived from the bare
table name. The schema itself is created by cp_realise migration 0002, which
also moved the tables there.
"""

SCHEMA = 'c_panel'


def c_panel_table(name: str) -> str:
    return f'{SCHEMA}"."{name}'
