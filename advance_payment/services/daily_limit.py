"""The Factory daily limit: Ziyaul's cap on one day's Factory payment requests.

Agreed with the user 2026-10-15/16. One place says what counts, so the
screen, the request form and the refusal can never disagree:

  A FACTORY REQUEST  Department (budget head) Factory or FACT_COM, OR
                     Payment Purpose Packaging Material, Raw Material (other
                     than oil), Semi-Finished Goods or Fixed Asset
                     Consumables / Spares — in any department, of any type.
                     Oil Purchase never counts.
  WHICH COMPANIES    Oil and Beverages, each its own limit (Mart has no factory).
  WHICH DAY          The day it was raised, on India's clock.
  WHAT IT USES       Its amount while it is alive: in approval, returned to
                     its creator, or completed. A rejected or withdrawn
                     request gives its amount back.
  NO LIMIT TODAY     The last one set before today carries forward; until
                     Ziyaul has ever set one, there is none.
  WHO SETS IT        Ziyaul alone (`hierarchy.PEOPLE['ziyaul']`, or the
                     `ADVANCE_PAYMENT_FACTORY_LIMIT_SETTER` setting).

Raising a request that would take the day past its limit is refused, and so
is an edit that adds to what a request uses. The check locks the limit row,
so two requests raised at the same moment cannot both slip under it.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction

from advance_payment.models import (
    AdvanceRequest, FactoryDailyLimit, FactoryDailyLimitChange, RequestStatus,
)
from advance_payment.purposes import FACTORY_BUDGETS
from advance_payment.services import clock
from advance_payment.services.payout import _inr

#: Companies with a factory, and so a limit.
LIMITED_COMPANIES = ('OIL', 'BEVERAGES')
COMPANY_LABEL = {'OIL': 'Oil', 'BEVERAGES': 'Beverages'}

#: Purposes that are factory spending whatever the department.
FACTORY_PURPOSES = frozenset({'PACKING_MATERIAL', 'RAW_MATERIAL', 'SEMI_FINISHED', 'FA_CONSUMABLES'})
#: Never counted: oil purchases have their own control.
EXCLUDED_PURPOSE = 'OIL_PURCHASE'

#: A request still using its amount.
LIVE_STATUSES = (RequestStatus.IN_APPROVAL, RequestStatus.RETURNED, RequestStatus.COMPLETED)

#: How many past days the screen shows, limit against raised.
HISTORY_DAYS = 14


def enabled():
    """Switched off for now (user, 2026-10-16): nothing is refused until the
    `ADVANCE_PAYMENT_FACTORY_LIMIT` setting is True."""
    return bool(getattr(settings, 'ADVANCE_PAYMENT_FACTORY_LIMIT', False))


class LimitInvalid(Exception):
    """Setting the limit broke a rule; the message says which."""


def setter_username():
    from advance_payment import hierarchy

    return str(getattr(settings, 'ADVANCE_PAYMENT_FACTORY_LIMIT_SETTER', '') or hierarchy.PEOPLE['ziyaul'])


def may_set(user):
    """Only Ziyaul sets the limit (user, 2026-10-15)."""
    return bool(user and user.is_authenticated and user.get_username().lower() == setter_username().lower())


def counts(company, budget_code, purpose_code):
    """Whether a request with these answers is a Factory request under the limit."""
    if company not in LIMITED_COMPANIES or purpose_code == EXCLUDED_PURPOSE:
        return False
    return budget_code in FACTORY_BUDGETS or purpose_code in FACTORY_PURPOSES


def _day_bounds(day):
    start = datetime.combine(day, time.min, tzinfo=clock.INDIA)
    return start, start + timedelta(days=1)


def raised_on(advance):
    """The India day a request was raised: the day whose limit it uses."""
    return advance.created_on.astimezone(clock.INDIA).date()


def requests_on(company, day, *, exclude_request=None):
    """The day's live Factory requests in `company`."""
    from django.db.models import Q

    start, end = _day_bounds(day)
    qs = (AdvanceRequest.objects
          .filter(company=company, created_on__gte=start, created_on__lt=end, status__in=LIVE_STATUSES)
          .filter(Q(budget_code__in=FACTORY_BUDGETS) | Q(purpose_code__in=FACTORY_PURPOSES))
          .exclude(purpose_code=EXCLUDED_PURPOSE))
    if exclude_request is not None:
        qs = qs.exclude(pk=exclude_request)
    return qs


def used_on(company, day, *, exclude_request=None):
    from django.db.models import Sum

    total = requests_on(company, day, exclude_request=exclude_request).aggregate(s=Sum('amount'))['s']
    return Decimal(total or 0).quantize(Decimal('0.01'))


def limit_for(company, day, *, lock=False):
    """The limit in force on `day`: that day's, else the last one set before it. None if never set."""
    qs = FactoryDailyLimit.objects.filter(company=company, day__lte=day).order_by('-day')
    if lock:
        qs = qs.select_for_update()
    return qs.first()


def problems(company, budget_code, purpose_code, amount, *, day, exclude_request=None):
    """Why a request of `amount` cannot use `day`'s limit: `[message]`, or `[]`.

    Locks the limit row: call inside the transaction that saves the request.
    """
    if amount is None or not counts(company, budget_code, purpose_code):
        return []
    limit = limit_for(company, day, lock=True)
    if limit is None:
        return []
    used = used_on(company, day, exclude_request=exclude_request)
    if used + amount <= limit.amount:
        return []
    left = max(limit.amount - used, Decimal('0'))
    return [f"This would take {COMPANY_LABEL[company]}'s Factory requests for {day:%d %b %Y} past the "
            f"daily limit Ziyaul set: limit {_inr(limit.amount)}, already raised {_inr(used)}, "
            f"left {_inr(left)} — this request is {_inr(amount)}, {_inr(used + amount - limit.amount)} over."]


def check_new(fields):
    """A request being raised: today's limit."""
    if not enabled():
        return []
    return problems(fields.get('company'), fields.get('budget_code', ''), fields.get('purpose_code', ''),
                    fields.get('amount'), day=clock.today())


def check_edit(advance, fields):
    """An edit is checked only when it adds to what the request uses, against
    the day it was raised: an unchanged request is never refused because the
    limit was lowered after it."""
    if not enabled():
        return []
    company = fields.get('company', advance.company)
    budget, purpose = fields.get('budget_code', advance.budget_code), fields.get('purpose_code', advance.purpose_code)
    amount = fields.get('amount', advance.amount)
    before = advance.amount if counts(advance.company, advance.budget_code, advance.purpose_code) else Decimal('0')
    if not counts(company, budget, purpose) or (company == advance.company and amount <= before):
        return []
    return problems(company, budget, purpose, amount, day=raised_on(advance), exclude_request=advance.pk)


@transaction.atomic
def set_limit(company, amount, *, user, day=None):
    """Ziyaul sets `day`'s limit (today by default; a later day may be set ahead)."""
    if not may_set(user):
        raise LimitInvalid('Only Ziyaul sets the Factory daily limit.')
    if company not in LIMITED_COMPANIES:
        raise LimitInvalid('The Factory daily limit is for Oil and Beverages only.')
    today = clock.today()
    day = day or today
    if day < today:
        raise LimitInvalid('A past day\'s limit cannot be changed.')
    try:
        value = Decimal(str(amount)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise LimitInvalid('Enter the limit as an amount in rupees.') from None
    if value <= 0:
        raise LimitInvalid('Enter a limit above zero.')
    row = FactoryDailyLimit.objects.select_for_update().filter(company=company, day=day).first()
    old = row.amount if row else None
    if row is None:
        row = FactoryDailyLimit.objects.create(company=company, day=day, amount=value, set_by=user)
    elif old != value:
        row.amount, row.set_by, row.set_on = value, user, clock.now()
        row.save(update_fields=['amount', 'set_by', 'set_on'])
    if old != value:
        FactoryDailyLimitChange.objects.create(limit=row, old_amount=old, new_amount=value, changed_by=user)
    return row


def _who(user):
    return (getattr(user, 'name', '') or user.get_username()) if user else ''


def summary(company, *, user, day=None):
    """What the screen and the form show for `company` on `day` (today by default)."""
    day = day or clock.today()
    limit = limit_for(company, day)
    used = used_on(company, day)
    rows = list(requests_on(company, day).select_related('created_by').order_by('-created_on'))
    history = []
    for back in range(1, HISTORY_DAYS + 1):
        past = day - timedelta(days=back)
        past_limit = limit_for(company, past)
        history.append({'day': past.isoformat(), 'limit': str(past_limit.amount) if past_limit else None,
                        'used': str(used_on(company, past))})
    changes = (FactoryDailyLimitChange.objects.filter(limit__company=company)
               .select_related('limit', 'changed_by')[:20])
    return {
        'company': company,
        'day': day.isoformat(),
        'limit': None if limit is None else {
            'amount': str(limit.amount), 'day': limit.day.isoformat(), 'carried': limit.day != day,
            'set_by': _who(limit.set_by), 'set_on': limit.set_on.isoformat(),
        },
        'used': str(used),
        'left': None if limit is None else str(max(limit.amount - used, Decimal('0'))),
        'requests': [{
            'id': r.pk, 'request_no': r.request_no, 'partner_name': r.partner_name, 'amount': str(r.amount),
            'budget_code': r.budget_code, 'purpose_label': r.purpose_label, 'status': r.status,
            'raised_by': _who(r.created_by), 'raised_on': r.created_on.isoformat(),
        } for r in rows],
        'history': history,
        'changes': [{'day': c.limit.day.isoformat(), 'old': None if c.old_amount is None else str(c.old_amount),
                     'new': str(c.new_amount), 'by': _who(c.changed_by), 'on': c.changed_on.isoformat()}
                    for c in changes],
        'enabled': enabled(),
        'can_set': may_set(user),
        'setter': setter_username(),
    }
