"""Startup validation for the cash-account configuration.

`SAP_CASH_PARENT_ACCOUNT` names the chart-of-accounts node whose postable
children are the company's cash drawers. Get it wrong and the CASH dropdown is
silently empty — a payment cannot be entered and nothing says why.

DELIBERATELY OFFLINE. These checks read settings only; they never open a HANA
connection. Startup must not depend on SAP being reachable: `manage.py check`
runs on every deploy and in CI, and a SAP outage must not stop the application
from booting when the configuration itself is perfectly correct.

Verifying the node against each company database — that it exists and is a
summary account — needs SAP, so it lives in the `validate_cash_parent`
management command, which an operator runs deliberately after changing the
setting.

Registered from `PaymentsConfig.ready()`, the same way `orders.checks` is.
"""

from django.conf import settings
from django.core.checks import Warning, register

CASH_PARENT_MISSING_ID = 'payments.W001'
CASH_PARENT_MALFORMED_ID = 'payments.W002'


@register()
def check_cash_parent_account(app_configs, **kwargs):
    """`SAP_CASH_PARENT_ACCOUNT` is present and looks like an account code."""
    value = str(getattr(settings, 'SAP_CASH_PARENT_ACCOUNT', '') or '').strip()

    if not value:
        return [
            Warning(
                'SAP_CASH_PARENT_ACCOUNT is not set, so no cash accounts can '
                'be offered when entering a CASH payment.',
                hint=(
                    'Set it to the chart-of-accounts node whose children are '
                    'the cash drawers (OACT."FatherNum"), then confirm it with '
                    '`python manage.py validate_cash_parent`.'
                ),
                id=CASH_PARENT_MISSING_ID,
            )
        ]

    # An account code, not a name: the value is compared against
    # OACT."FatherNum". Whitespace or a stray label would match nothing and
    # produce the same empty dropdown as leaving it unset.
    if value != value.strip() or ' ' in value:
        return [
            Warning(
                f'SAP_CASH_PARENT_ACCOUNT ({value!r}) does not look like an '
                'account code.',
                hint='Use the bare code, e.g. the parent of the cash drawers.',
                id=CASH_PARENT_MALFORMED_ID,
            )
        ]

    return []
