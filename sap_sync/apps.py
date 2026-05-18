from django.apps import AppConfig


class SapSyncConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sap_sync'
    verbose_name = 'SAP Sync'
    
    def ready(self):
        """Start scheduler when Django starts (only in main process)"""
        import os
        
        # Only start scheduler in main process (not in migrations, shell, etc.)
        if os.environ.get('RUN_MAIN') == 'true':
            try:
                from .scheduler import start_scheduler
                start_scheduler()
            except Exception as e:
                print(f"Failed to start SAP Sync scheduler: {e}")
