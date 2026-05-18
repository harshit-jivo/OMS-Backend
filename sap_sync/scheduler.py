import logging
from datetime import datetime, timedelta
from django.utils import timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from django_apscheduler.jobstores import DjangoJobStore

from .models import SyncSchedule
from .services import SyncService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()
scheduler.add_jobstore(DjangoJobStore(), "default")


def run_scheduled_sync(schedule_id):
    """Execute a scheduled sync job"""
    try:
        schedule = SyncSchedule.objects.get(pk=schedule_id)
        
        if not schedule.is_active:
            logger.info(f"Schedule {schedule.name} is inactive, skipping")
            return
        
        logger.info(f"Starting scheduled sync: {schedule.name}")
        
        sync_service = SyncService(triggered_by='scheduled')
        
        if schedule.sync_type == 'ALL':
            result = sync_service.sync_all()
        elif schedule.sync_type == 'PRODUCT':
            result = sync_service.sync_products()
        elif schedule.sync_type == 'PARTY':
            result = sync_service.sync_parties()
        elif schedule.sync_type == 'PARTY_ADDRESS':
            result = sync_service.sync_party_addresses()
        else:
            result = sync_service.sync_all()
        
        # Update schedule timestamps
        schedule.last_run = timezone.now()
        schedule.save()
        
        logger.info(f"Scheduled sync completed: {schedule.name} - {result}")
        
    except SyncSchedule.DoesNotExist:
        logger.error(f"Schedule {schedule_id} not found")
    except Exception as e:
        logger.error(f"Scheduled sync failed: {str(e)}")


def add_schedule_job(schedule):
    """Add a job to the scheduler based on schedule configuration"""
    job_id = f"sync_schedule_{schedule.id}"
    
    # Remove existing job if any
    try:
        scheduler.remove_job(job_id)
    except:
        pass
    
    if not schedule.is_active:
        return
    
    if schedule.frequency == 'HOURLY':
        trigger = IntervalTrigger(hours=1)
    elif schedule.frequency == 'DAILY':
        trigger = CronTrigger(hour=schedule.hour, minute=0)
    elif schedule.frequency == 'WEEKLY':
        trigger = CronTrigger(day_of_week='mon', hour=schedule.hour, minute=0)
    elif schedule.frequency == 'CUSTOM':
        trigger = IntervalTrigger(minutes=schedule.custom_interval_minutes)
    else:
        trigger = IntervalTrigger(hours=24)
    
    scheduler.add_job(
        run_scheduled_sync,
        trigger=trigger,
        id=job_id,
        args=[schedule.id],
        name=schedule.name,
        replace_existing=True
    )
    
    # Calculate and save next run time
    next_run = trigger.get_next_fire_time(None, timezone.now())
    schedule.next_run = next_run
    schedule.save(update_fields=['next_run'])
    
    logger.info(f"Added schedule job: {schedule.name} ({job_id})")


def remove_schedule_job(schedule_id):
    """Remove a job from the scheduler"""
    job_id = f"sync_schedule_{schedule_id}"
    try:
        scheduler.remove_job(job_id)
        logger.info(f"Removed schedule job: {job_id}")
    except:
        pass


def start_scheduler():
    """Start the scheduler and load all active schedules"""
    if scheduler.running:
        return
    
    # Load all active schedules
    active_schedules = SyncSchedule.objects.filter(is_active=True)
    for schedule in active_schedules:
        add_schedule_job(schedule)
    
    scheduler.start()
    logger.info("SAP Sync Scheduler started")


def stop_scheduler():
    """Stop the scheduler"""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("SAP Sync Scheduler stopped")


def refresh_schedules():
    """Refresh all schedule jobs (call after schedule changes)"""
    # Remove all sync jobs
    for job in scheduler.get_jobs():
        if job.id.startswith('sync_schedule_'):
            scheduler.remove_job(job.id)
    
    # Re-add active schedules
    active_schedules = SyncSchedule.objects.filter(is_active=True)
    for schedule in active_schedules:
        add_schedule_job(schedule)
    
    logger.info("Schedules refreshed")
