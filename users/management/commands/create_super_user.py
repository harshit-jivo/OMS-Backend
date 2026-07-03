"""
Create (or update) the all-access OMS user `super@oms.com`.

Access model (see OMS-Frontend src/components/Sidebar.tsx): a user with the
`admin` role sees every page — `canSee = isAdmin || extra_pages.includes(key)`.
So this command assigns the `admin` role AND fills `extra_pages` with every
grantable page key (belt-and-suspenders), and flags Django is_staff/is_superuser.

Idempotent: re-running updates the existing user (and resets the password).

    python manage.py create_super_user
    python manage.py create_super_user --password "MyStrong@Pass1"
    python manage.py create_super_user --username super@oms.com --email super@oms.com
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from users.models import User, UserRole

# Mirror of OMS-Frontend/src/config/adminPages.ts -> GRANTABLE_PAGE_KEYS.
# The admin role already unlocks all pages; these are set too for completeness
# and in case any non-admin check reads extra_pages directly.
ALL_PAGE_KEYS = [
    "App_User",
    "Sap_Sync",
    "Party_Assignment",
    "Party_Product_Assignment",
    "Add_Scheme",
    "Order_Flow_Settings",
    "Product_Stock",
    "Reports",
]


class Command(BaseCommand):
    help = "Create/update super@oms.com with the admin role and all page permissions."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="super@oms.com",
                            help="Login username (default: super@oms.com)")
        parser.add_argument("--email", default="super@oms.com")
        parser.add_argument("--name", default="Super Admin")
        parser.add_argument("--password", default="Super@1234",
                            help="Password to set (default: Super@1234)")

    @transaction.atomic
    def handle(self, *args, **opts):
        username = opts["username"]
        password = opts["password"]

        role, role_created = UserRole.objects.get_or_create(
            name="admin",
            defaults={"display_name": "Admin", "is_active": True},
        )
        if not role.is_active:
            role.is_active = True
            role.save(update_fields=["is_active"])

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"name": opts["name"], "email": opts["email"]},
        )
        user.name = user.name or opts["name"]
        user.email = opts["email"]
        user.role = role
        user.extra_pages = list(ALL_PAGE_KEYS)
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Updated'} user '{username}' "
            f"(role=admin{' [role created]' if role_created else ''})."
        ))
        self.stdout.write(f"  Password set to: {password}")
        self.stdout.write(f"  extra_pages: {user.extra_pages}")
        self.stdout.write(self.style.WARNING("  Log in with the USERNAME above."))
