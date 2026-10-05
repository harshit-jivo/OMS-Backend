"""No models: the Control Panel's data lives in the `cp_realise` / `cp_dashboard`
apps (cpanel/), in the `c_panel` schema (cpanel/core/db.py).

This app's first port kept its own copies of the target and budget tables
(`control_panel_*`); nothing ever used them, and migration 0002 drops them.
"""
