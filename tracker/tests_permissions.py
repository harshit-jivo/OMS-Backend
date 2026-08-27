"""Tracker page-access rules.

`tracker` had zero tests across 3,905 lines and 15 models while owning an
8-stage document workflow — and `tracker_pages_for` is the single function
every tracker view and the whole frontend `pageAccess.ts` mirror depends on.

These pin the rules the module docstring states, including the two that are
easy to "fix" by mistake:

* superusers and the OMS `admin` role get NOTHING here, deliberately;
* `extra_roles` count, which they did not until 2026-08-27.

Run with::

    python manage.py test tracker --settings=OMS.test_settings
"""
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from users.models import User, UserRole

from .permissions import (
    ALL_TRACKER_PAGES,
    PAGE_ADMIN,
    PAGE_ALERTS,
    PAGE_AP,
    PAGE_ENTRY,
    PAGE_QUEUE,
    ROLE_PAGE_MAP,
    tracker_pages_for,
)


def _role(name):
    role, _ = UserRole.objects.get_or_create(
        name=name, defaults={'display_name': name, 'is_active': True})
    return role


def _user(username, role_name=None, extra=(), **kw):
    user = User.objects.create_user(
        username=username, password='pw', name=username,
        role=_role(role_name) if role_name else None, **kw)
    for name in extra:
        user.extra_roles.add(_role(name))
    return user


class PrimaryRoleTests(TestCase):

    def test_each_sub_role_gets_exactly_its_mapped_pages(self):
        for role_name, expected in ROLE_PAGE_MAP.items():
            user = _user(f'tp-{role_name}', role_name)
            self.assertEqual(tracker_pages_for(user), expected, role_name)

    def test_tracker_admin_sees_every_page(self):
        self.assertEqual(
            tracker_pages_for(_user('tp-admin', 'tracker_admin')),
            ALL_TRACKER_PAGES)

    def test_alerts_and_the_master_list_are_admin_only(self):
        """Stuck Alerts scans every stage in the company; the invoice master
        list is every invoice in it. Neither belongs to a queue user."""
        for role_name in ('tracker_entry', 'tracker_user', 'tracker_ap'):
            self.assertNotIn(PAGE_ALERTS, tracker_pages_for(
                _user(f'tp-al-{role_name}', role_name)), role_name)
            self.assertNotIn(PAGE_ADMIN, tracker_pages_for(
                _user(f'tp-ad-{role_name}', role_name)), role_name)

    def test_ap_entry_is_separate_from_document_tracking(self):
        """`tracker_ap` posts vendor invoices into SAP and has no reason to see
        the tracker queues — a distinct job, not a lesser tracker user."""
        pages = tracker_pages_for(_user('tp-ap', 'tracker_ap'))
        self.assertEqual(pages, {PAGE_AP})
        self.assertNotIn(PAGE_QUEUE, pages)


class NoSpecialCasingTests(TestCase):
    """The module is explicit that OMS admin and superuser mean nothing here.

    This is the rule most likely to be "corrected" by someone who assumes a
    superuser should see everything. It is intentional: tracker access is a job
    assignment, not a privilege level, and granting superuser must not silently
    hand someone the vendor-invoice workflow.
    """

    def test_the_oms_admin_role_gets_no_tracker_pages(self):
        self.assertEqual(tracker_pages_for(_user('tp-oms', 'admin')), set())

    def test_a_superuser_gets_no_tracker_pages(self):
        user = _user('tp-super', 'admin', is_superuser=True, is_staff=True)
        self.assertEqual(tracker_pages_for(user), set())

    def test_anonymous_and_roleless_users_get_nothing(self):
        self.assertEqual(tracker_pages_for(AnonymousUser()), set())
        self.assertEqual(tracker_pages_for(None), set())
        self.assertEqual(tracker_pages_for(_user('tp-none')), set())

    def test_an_unknown_role_gets_nothing(self):
        """Fails closed: a role absent from ROLE_PAGE_MAP grants no page."""
        self.assertEqual(tracker_pages_for(_user('tp-x', 'salesman')), set())


class ExtraRolesTests(TestCase):
    """The §7.4 bug.

    `role` is a single FK, so a manager who also runs the tracker had to give
    up `manager` — and with it their access to orders and reports — to hold
    `tracker_admin`. `extra_roles` exists to remove exactly that choice, and
    `users/models.py` states that every "does this user hold role X" check must
    consult both.

    This module read the FK alone, so the grant did nothing here while every
    other module honoured it.
    """

    def test_a_tracker_role_held_as_an_extra_role_grants_access(self):
        user = _user('tp-mgr', 'manager', extra=['tracker_admin'])
        self.assertEqual(tracker_pages_for(user), ALL_TRACKER_PAGES)

    def test_the_primary_role_still_works_alongside_an_extra_one(self):
        user = _user('tp-both', 'tracker_user', extra=['tracker_ap'])
        self.assertEqual(tracker_pages_for(user), {PAGE_QUEUE, PAGE_AP})

    def test_pages_are_unioned_not_first_match(self):
        """Holding two roles must never grant less than holding one. A
        first-match-wins lookup over an unordered role set would do exactly
        that, and non-deterministically.
        """
        user = _user('tp-union', 'tracker_entry', extra=['tracker_ap'])
        self.assertEqual(tracker_pages_for(user),
                         {PAGE_ENTRY, PAGE_QUEUE, PAGE_AP})

    def test_a_non_tracker_extra_role_grants_nothing(self):
        user = _user('tp-noise', 'tracker_user', extra=['payment_approver'])
        self.assertEqual(tracker_pages_for(user), {PAGE_QUEUE})
