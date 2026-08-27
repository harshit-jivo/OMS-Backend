"""Saved order templates: signature, duplicate detection, save-if-unique.

Lifted out of `orders/views.py` (plan item 3.2). A template is a remembered
order a user can re-place, so the only real question here is whether a new
order is materially the SAME as one already saved — which is a business rule
about what counts as "the same order", not an HTTP concern.

`_build_order_template_signature` is where that judgement lives.
"""
from orders.models import Template




def _normalize_template_number(value):
    try:
        if value in (None, ''):
            return 0.0
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def _build_order_template_signature(order):
    item_signatures = []

    for item in order.items.all().prefetch_related('schemes').order_by('id'):
        scheme_signatures = sorted(
            (
                getattr(item_scheme, 'scheme_id', None),
                _normalize_template_number(getattr(item_scheme, 'qty_scheme', 0)),
            )
            for item_scheme in item.schemes.all()
            if getattr(item_scheme, 'scheme_id', None)
        )

        # Fallback for older rows that only use the single scheme fields.
        if not scheme_signatures and getattr(item, 'scheme_id', None):
            scheme_signatures = [(
                getattr(item, 'scheme_id', None),
                _normalize_template_number(getattr(item, 'qty_scheme', 0)),
            )]

        item_signatures.append((
            item.item_code or '',
            item.sub_group or '',
            item.item_type or '',
            _normalize_template_number(item.qty),
            _normalize_template_number(item.pcs),
            _normalize_template_number(item.boxes),
            _normalize_template_number(item.ltrs),
            _normalize_template_number(item.price_list_basic),
            _normalize_template_number(item.basic_price),
            tuple(scheme_signatures),
        ))

    return tuple(sorted(item_signatures))


def _has_duplicate_template(user, order):
    order_signature = _build_order_template_signature(order)
    existing_templates = (
        Template.objects.filter(user=user, order__card_code=order.card_code)
        .exclude(order=order)
        .select_related('order')
        .prefetch_related('order__items__schemes')
    )

    for template in existing_templates:
        if _build_order_template_signature(template.order) == order_signature:
            return True

    return False


def _get_order_template_sub_group(order):
    sub_groups = [
        str(sub_group or '').strip()
        for sub_group in order.items.values_list('sub_group', flat=True).distinct()
        if str(sub_group or '').strip()
    ]
    return ', '.join(sorted(sub_groups)) or None


def _save_template_if_unique(user, order):
    if not user:
        return

    if _has_duplicate_template(user, order):
        return

    sub_group = _get_order_template_sub_group(order)
    template, created = Template.objects.get_or_create(
        user=user,
        order=order,
        defaults={'sub_group': sub_group},
    )
    if not created and template.sub_group != sub_group:
        template.sub_group = sub_group
        template.save(update_fields=['sub_group'])
