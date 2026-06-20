"""Model-level audit capture.

Writes simple AuditLog rows whenever an admin-managed record is created,
updated or deleted. For updates, one row is written per changed field so the
old and new values land in their own columns. Connected only to the models in
``AUDITED_MODELS`` so normal sales/order traffic is never logged.
"""
import logging

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save

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

# Many-to-many relations to audit, as (owner model label, field name).
# These don't fire normal save signals, so they're handled via m2m_changed,
# which produces a single consolidated row per change (all added/removed at once).
AUDITED_M2M = [
    ('users.User', 'main_groups'),
    ('users.User', 'states'),
]

_audited_classes = set()
_m2m_info = {}  # through model -> (owner friendly name, field name)

# Bookkeeping fields the views set automatically (who/when), not a user edit.
# The audit log already records who did it, so these would just be noise.
IGNORED_FIELDS = {'assigned_by', 'created_by', 'updated_by'}


def _friendly(model_cls):
    return model_cls._meta.verbose_name.title()


def _instance_label(instance):
    """Readable label for the record. UserState's own __str__ is just ids."""
    if type(instance)._meta.label == 'users.UserState':
        try:
            return f'{instance.user.username} - {instance.state.name}'
        except Exception:
            pass
    return str(instance)


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
    # old_value / new_value are unlimited text; keep a high cap only as a guard
    # against accidental blobs, so long lists (e.g. sub_group) show in full.
    return text if len(text) <= 5000 else text[:4997] + '...'


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


def _entry(model_cls, instance, record, action):
    """Get (or start) the buffered change-set for one record. During a request
    all handlers share the same entry (keyed by model + pk) so everything lands
    in one row; outside a request a throwaway entry is returned and written
    immediately by the caller."""
    rec = {
        'page': context.get().get('page') or page_for_model(model_cls),
        'record': str(record)[:255],
        'action': action,
        'changes': {},  # field -> {'old': ..., 'new': ...}
    }
    if not context.is_active():
        return rec, False  # caller writes it now
    buf = context.buffer()
    key = f'{model_cls._meta.label}:{getattr(instance, "pk", "")}'
    existing = buf.get(key)
    if existing is None:
        buf[key] = rec
        return rec, True
    # Merge into the existing entry; escalate the action sensibly.
    if action == 'Created':
        existing['action'] = 'Created'
    elif action == 'Deleted':
        existing['action'] = 'Deleted'
    return existing, True


def _render(rec):
    """Return (field, old_value, new_value) for a buffered change-set."""
    changes = rec['changes']
    if rec['action'] == 'Created':
        new_value = '; '.join(f'{f}: {c["new"]}' for f, c in changes.items()
                              if c.get('new') not in (None, ''))
        return '', '', new_value
    if rec['action'] == 'Deleted':
        return '', '', ''
    field = ', '.join(changes.keys())
    old_value = '; '.join(f'{f}: {c["old"]}' for f, c in changes.items()
                          if c.get('old') not in (None, ''))
    new_value = '; '.join(f'{f}: {c["new"]}' for f, c in changes.items()
                          if c.get('new') not in (None, ''))
    return field, old_value, new_value


def _write_row(rec):
    from .models import AuditLog
    if rec['action'] == 'Updated' and not rec['changes']:
        return  # nothing actually changed
    field, old_value, new_value = _render(rec)
    ctx = context.get()
    user = ctx.get('user')
    try:
        AuditLog.objects.create(
            user=user if getattr(user, 'pk', None) else None,
            username=getattr(user, 'username', '') or '',
            page=rec['page'],
            action=rec['action'],
            record=rec['record'],
            field=field[:100],
            old_value=old_value,
            new_value=new_value,
        )
    except Exception:
        logger.exception('Failed to write audit log')


def flush():
    """Write one row per buffered record. Called by the middleware at the end
    of a request. Returns the number of rows written."""
    buf = context.buffer()
    if not buf:
        return 0
    count = 0
    for rec in buf.values():
        _write_row(rec)
        count += 1
    return count


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
    record = f'{_friendly(sender)}: {_instance_label(instance)}'

    if created:
        rec, buffered = _entry(sender, instance, record, 'Created')
        pk_name = sender._meta.pk.name
        for field_name, value in _field_values(instance).items():
            if field_name == pk_name:
                continue
            shown = '(hidden)' if 'password' in field_name.lower() else _show(value)
            if shown == '':
                continue
            rec['changes'][field_name] = {'old': '', 'new': shown}
        if not buffered:
            _write_row(rec)
        return

    changes = list(_changed(getattr(instance, '_audit_old', None) or {}, _field_values(instance)))
    if not changes:
        return
    rec, buffered = _entry(sender, instance, record, 'Updated')
    for field_name, old, new in changes:
        rec['changes'][field_name] = {'old': old, 'new': new}
    if not buffered:
        _write_row(rec)


def log_delete(sender, instance, **kwargs):
    if sender not in _audited_classes:
        return
    record = f'{_friendly(sender)}: {_instance_label(instance)}'
    rec, buffered = _entry(sender, instance, record, 'Deleted')
    if not buffered:
        _write_row(rec)


def log_m2m(sender, instance, action, pk_set, model, **kwargs):
    """Audit add/remove on a many-to-many relation (e.g. main_groups, states).
    Merges into the same record entry so it shares the row with field changes."""
    if action not in ('post_add', 'post_remove') or not pk_set:
        return
    info = _m2m_info.get(sender)
    if not info:
        return
    owner_friendly, field_name = info
    try:
        names = ', '.join(str(obj) for obj in model.objects.filter(pk__in=pk_set))
    except Exception:
        names = ', '.join(str(pk) for pk in pk_set)

    record = f'{owner_friendly}: {instance}'
    rec, buffered = _entry(type(instance), instance, record, 'Updated')
    cell = rec['changes'].setdefault(field_name, {'old': '', 'new': ''})
    if action == 'post_add':
        cell['new'] = f'added: {names}'
    else:
        cell['old'] = f'removed: {names}'
    if not buffered:
        _write_row(rec)


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

    for label, field_name in AUDITED_M2M:
        try:
            model_cls = apps.get_model(label)
            through = getattr(model_cls, field_name).through
        except Exception:
            logger.warning('Audit: could not resolve m2m %s.%s', label, field_name)
            continue
        _m2m_info[through] = (_friendly(model_cls), field_name)
        m2m_changed.connect(log_m2m, sender=through, dispatch_uid=f'audit_m2m_{label}_{field_name}')
