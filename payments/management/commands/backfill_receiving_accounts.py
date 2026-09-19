"""Stamp the receiving account onto payment lines that still resolve by mapping.

    python manage.py backfill_receiving_accounts --dry-run
    python manage.py backfill_receiving_accounts

WHY THIS EXISTS
---------------
Receipts raised before the account picker carry no `account_key`, and their
G/L is worked out at posting time from `PaymentMethodMapping` — the admin's
one-account-per-method table the picker replaced. That fallback is the last
thing keeping the mapping table alive.

This writes the account the fallback WOULD choose onto the line itself, so the
line answers for itself and the mapping is never consulted again. Once no line
needs it, the fallback and the table can go.

WHICH LINES IT TOUCHES, AND WHY NOT THE REST
--------------------------------------------
Only lines whose account is still going to be READ:

  * a receipt that has not posted — submit validation and the SAP post both
    resolve its accounts (`services._bank_accounts_for`);
  * a CASH line on a receipt not yet banked — creating a deposit over it reads
    the drawer it came from (`services.cash_sources_for_receipts`).

A line on a receipt that has already posted AND already been banked is left
alone, deliberately. Nothing will ever resolve it again, and the mapping has
been edited since some of those receipts posted (2026-09-05, while the oldest
receipt dates from July) — so writing today's mapping onto them would record
an account SAP may never have used. Leaving a historical line honestly blank
is better than filling it with a plausible guess.

For the lines it DOES touch, the value is exact rather than guessed: the same
mapping, resolved through the same SAP lists, in the same moment it would have
been resolved anyway. Running this changes where no money goes.

SAFE TO RE-RUN. A line that already carries an account is skipped, so the
second run reports zero.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from payments import bank_master
from payments.models import (BankDepositLine, PaymentMethodEntry,
                             PaymentReceipt)
from payments.serializers import (RECEIVING_ACCOUNT_FIELDS,
                                  resolve_receiving_account)

#: A receipt in one of these has finished with SAP; nothing re-resolves it.
SETTLED = (
    PaymentReceipt.Status.POSTED,
    PaymentReceipt.Status.CANCELLED,
    PaymentReceipt.Status.CANCELLED_IN_SAP,
)


class Command(BaseCommand):
    help = ('Write the legacy mapping\'s account onto keyless payment lines '
            'that still need one, so PaymentMethodMapping can be retired.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change and write nothing.')
        parser.add_argument('--company', default='',
                            help='Limit to one company.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        company = (options['company'] or '').strip().upper()

        lines = self._lines_that_still_resolve(company)
        if not lines:
            self.stdout.write(self.style.SUCCESS(
                'Nothing to do: no keyless line is still resolved by the '
                'mapping.'))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            'WOULD WRITE' if dry_run else 'WRITING'))

        written, failed, unmapped = 0, [], []
        for entry, receipt, why in lines:
            key = self._legacy_key(receipt.company, entry.method)
            if not key:
                # The mapping has no answer for this method either, so the line
                # is NOT depending on it: today it fails validation with "no
                # account configured", and after the table goes it will fail
                # with "choose a receiving account". Reported, not blocking.
                unmapped.append((receipt.receipt_no, entry.method,
                                 receipt.status))
                continue
            try:
                snapshot = resolve_receiving_account(
                    receipt.company, entry.method, key)
            except Exception as exc:                      # noqa: BLE001
                # The mapping names an account SAP no longer offers — frozen,
                # renamed, removed. Reported rather than forced: the line needs
                # a human to pick a current account.
                failed.append((receipt.receipt_no, entry.method,
                               f'{key} did not resolve ({exc})'))
                continue
            if not snapshot:
                failed.append((receipt.receipt_no, entry.method,
                               f'{key} resolved to nothing'))
                continue

            self.stdout.write(
                f'  {receipt.receipt_no:32} {entry.method:7} -> '
                f'{snapshot["account_key"]:16} '
                f'({snapshot["receiving_bank_name"] or snapshot["gl_account"]}) '
                f'[{why}]')
            if not dry_run:
                with transaction.atomic():
                    for field in RECEIVING_ACCOUNT_FIELDS:
                        setattr(entry, field, snapshot.get(field, ''))
                    entry.save(update_fields=list(RECEIVING_ACCOUNT_FIELDS))
            written += 1

        self.stdout.write('')
        if unmapped:
            self.stdout.write(self.style.WARNING(
                f'{len(unmapped)} line(s) have NO mapping to inherit — they '
                f'could not post today either:'))
            for number, method, status in unmapped:
                self.stdout.write(self.style.WARNING(
                    f'  {number:32} {method:7} ({status})'))
            self.stdout.write(
                '  These do not depend on the mapping. Whoever owns them '
                'chooses an account in the app, as any new receipt does.')
            self.stdout.write('')

        if failed:
            self.stdout.write(self.style.ERROR(
                f'{len(failed)} line(s) could NOT be resolved:'))
            for number, method, reason in failed:
                self.stdout.write(self.style.ERROR(
                    f'  {number:32} {method:7} {reason}'))
            self.stdout.write('')
            self.stdout.write(
                'Those lines still depend on the mapping. Open each receipt '
                'and choose a receiving account before retiring the table.')

        deposits_done, deposits_failed = self._freeze_deposit_sources(
            company, dry_run)
        failed.extend(deposits_failed)

        verb = 'would be written' if dry_run else 'written'
        self.stdout.write(self.style.SUCCESS(
            f'{written} line(s) and {deposits_done} deposit source(s) '
            f'{verb}.'))
        if dry_run:
            self.stdout.write('Dry run — nothing was changed.')
        if failed:
            raise CommandError(
                f'{len(failed)} line(s) unresolved; the mapping is still '
                f'required.')

    # -- deposits ----------------------------------------------------------

    def _freeze_deposit_sources(self, company, dry_run):
        """Record the drawer each unsettled deposit empties.

        A deposit submitted before the source was frozen carries none, and
        posting falls back to the CASH mapping — the last deposit-side reader
        of the table.

        The value is NOT taken from the mapping. It is read from the receipts
        the deposit actually banks, through the same
        `services.deposit_cash_sources` the submit path uses, so the drawer
        credited is provably the one those receipts debited. That is stricter
        than the mapping ever was: the mapping could only ever name one account
        per company, whatever the receipts said.

        Settled deposits are left alone — nothing re-resolves them.
        """
        from payments import services
        from payments.models import BankDeposit

        SETTLED_DEPOSITS = (
            BankDeposit.Status.POSTED,
            BankDeposit.Status.CANCELLED,
            BankDeposit.Status.CANCELLED_IN_SAP,
        )
        deposits = (BankDeposit.objects
                    .filter(source_gl_account='')
                    .exclude(status__in=SETTLED_DEPOSITS)
                    .order_by('id'))
        if company:
            deposits = deposits.filter(company=company)

        done, failed = 0, []
        for deposit in deposits:
            try:
                source = services.check_one_cash_source(
                    services.deposit_cash_sources(deposit))
            except Exception as exc:                      # noqa: BLE001
                failed.append((deposit.deposit_no, 'SOURCE', str(exc)))
                continue
            if not source:
                # Cheque-only: there is no drawer to credit and SAP receives
                # nothing. Correctly blank, not a failure.
                continue

            self.stdout.write(
                f'  {deposit.deposit_no:32} {"SOURCE":7} -> {source:16} '
                f'(from its own receipts) [deposit]')
            if not dry_run:
                deposit.source_gl_account = source
                deposit.save(update_fields=['source_gl_account', 'updated_at'])
            done += 1
        return done, failed

    # -- selection ---------------------------------------------------------

    def _lines_that_still_resolve(self, company):
        """(entry, receipt, why) for every keyless line still read."""
        banked = set(BankDepositLine.objects.values_list('receipt_id',
                                                         flat=True))
        entries = (PaymentMethodEntry.objects
                   .filter(account_key='', gl_account='')
                   .select_related('receipt')
                   .order_by('receipt_id', 'id'))
        if company:
            entries = entries.filter(receipt__company=company)

        found = []
        for entry in entries:
            receipt = entry.receipt
            if receipt.status not in SETTLED:
                found.append((entry, receipt, 'awaiting SAP'))
            elif (entry.method == PaymentMethodEntry.Method.CASH
                    and receipt.pk not in banked):
                found.append((entry, receipt, 'depositable cash'))
            # else: posted and banked — nothing reads it again.
        return found

    def _legacy_key(self, company, method):
        """The key the mapping would have resolved this method through.

        CASH maps to a G/L account directly; a banked tender maps to a house
        bank, whose key is `BANKCODE:GL`. Exactly the two shapes
        `resolve_receiving_account` expects, which is what makes the snapshot
        identical to what posting would have produced.
        """
        resolver = bank_master.PaymentAccountResolver(company)
        if method == PaymentMethodEntry.Method.CASH:
            return resolver.cash_gl()
        found = resolver.resolve(method)
        if not found:
            return ''
        return found.get('bank_key') or found.get('gl_account') or ''
