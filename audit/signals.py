"""Model-level audit capture.

Writes simple AuditLog rows whenever an admin-managed record is created,
updated or deleted. For updates, one row is written per changed field so the
old and new values land in their own columns. Connected only to the models in
``AUDITED_MODELS`` so normal sales/order traffic is never logged.
"""
import logging

from django.db.models.signals import post_delete, post_save, pre_save

from . import context
from .pages import page_for_model

logger = logging.getLogger(__name__)

AUDITED_MODELS = [
    'users.User',
    'users.UserPartyAssignment',
    'users.PartyProductAssignment',
    'users.SchemeProduct',
    'sap_sync.SyncLog',
]

_audited_classes = set()

# Bookkeeping fields the views set automatically (who/when), not a user edit.
# The audit log already records who did it, so these would just be noise.
IGNORED_FIELDS = {'assigned_by', 'created_by', 'updated_by'}


def _friendly(model_cls):
    return model_cls._meta.verbose_name.title()


def _field_values(instance):
    """Field -> value, skipping auto-managed timestamps (auto_now /
    auto_now_add). Those change on every save and aren't a user edit, so
    re-saving an unchanged record produces no diff (and therefore no log)."""
    values = {}
    for field in instance._meta.fields:
        if getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
            continue
        if field.name in IGNORED_FIELDS:
            continue
        try:
            values[field.name] = field.value_from_object(instance)
        except Exception:
            continue
    return values


def _show(value):
    if value is None:
        return ''
    text = str(value)
    return text if len(text) <= 200 else text[:197] + '...'


def _changed(old_values, new_values):
    """Yield (field, old, new) for every changed field."""
    for key in sorted(set(old_values) | set(new_values)):
        old = old_values.get(key)
        new = new_values.get(key)
        if old == new:
            continue
        if 'password' in key.lower():
            yield key, '(hidden)', '(hidden)'
        else:
            yield key, _show(old), _show(new)


def _write(model_cls, action, record, field='', old_value='', new_value=''):
    from .models import AuditLog
    ctx = context.get()
    user = ctx.get('user')
    try:
        AuditLog.objects.create(
            user=user if getattr(user, 'pk', None) else None,
            username=getattr(user, 'username', '') or '',
            page=ctx.get('page') or page_for_model(model_cls),
            action=action,
            record=str(record)[:255],
            field=field[:100],
            old_value=old_value,
            new_value=new_value,
        )
        context.mark_model_write()
    except Exception:
        logger.exception('Failed to write audit log for %s', model_cls)


def capture_old(sender, instance, **kwargs):
    if sender not in _audited_classes:
        return
    if not instance.pk:
        instance._audit_old = None
        return
    try:
        instance._audit_old = _field_values(sender.objects.get(pk=instance.pk))
    except Exception:
        instance._audit_old = None


def log_save(sender, instance, created, **kwargs):
    if sender not in _audited_classes:
        return
    record = f'{_friendly(sender)}: {instance}'
    if created:
        pk_name = sender._meta.pk.name
        parts = []
        for field_name, value in _field_values(instance).items():
            if field_name == pk_name:
                continue  # skip the auto-generated id
            if 'password' in field_name.lower():
                shown = '(hidden)'
            else:
                shown = _show(value)
            if shown == '':
                continue  # skip empty fields
            parts.append(f'{field_name}: {shown}')
        _write(sender, 'Created', record, new_value='; '.join(parts))
        return

    changes = list(_changed(getattr(instance, '_audit_old', None) or {}, _field_values(instance)))
    if not changes:
        return  # save() with nothing actually changed
    fields = ', '.join(field for field, _, _ in changes)
    old_value = '; '.join(f'{field}: {old}' for field, old, _ in changes)
    new_value = '; '.join(f'{field}: {new}' for field, _, new in changes)
    _write(sender, 'Updated', record, field=fields, old_value=old_value, new_value=new_value)


def log_delete(sender, instance, **kwargs):
    if sender not in _audited_classes:
        return
    _write(sender, 'Deleted', f'{_friendly(sender)}: {instance}')


def connect():
    """Resolve the audited models and wire up the handlers (from AuditConfig.ready)."""
    from django.apps import apps

    for label in AUDITED_MODELS:
        try:
            model_cls = apps.get_model(label)
        except Exception:
            logger.warning('Audit: could not resolve model %s', label)
            continue
        _audited_classes.add(model_cls)
        uid = f'audit_{label}'
        pre_save.connect(capture_old, sender=model_cls, dispatch_uid=uid + '_pre')
        post_save.connect(log_save, sender=model_cls, dispatch_uid=uid + '_post')
        post_delete.connect(log_delete, sender=model_cls, dispatch_uid=uid + '_del')
