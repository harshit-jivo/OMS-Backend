from django.apps import AppConfig


class SapSyncConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sap_sync'
    verbose_name = 'SAP Sync'

    # Phase 5.1 — the scheduler no longer starts here.
    #
    # `ready()` runs in EVERY process that loads Django: each web worker, and
    # also `migrate`, `shell`, `collectstatic` and the test runner. Starting a
    # background thread from it means the number of schedulers is whatever the
    # deployment happens to do, which is not a decision this file can make.
    #
    # The old code tried to narrow that with an `RUN_MAIN` check:
    #
    #     def ready(self):
    #         import os
    #         # Only start scheduler in main process (not in migrations, shell, etc.)
    #         if os.environ.get('RUN_MAIN') == 'true':
    #             try:
    #                 from .scheduler import start_scheduler
    #                 start_scheduler()
    #             except Exception as e:
    #                 print(f"Failed to start SAP Sync scheduler: {e}")
    #
    # `RUN_MAIN` is set by the Django dev-server autoreloader and by nothing
    # else, so under waitress, gunicorn or IIS the condition was never true and
    # the scheduler never started. Production has 0 scheduled sync runs on
    # record to show for it. The bare `except` printing to stdout meant that
    # was also completely silent.
    #
    # Scheduling now lives in `manage.py run_scheduler`, a dedicated process
    # holding a Postgres advisory lock so exactly one copy runs.
