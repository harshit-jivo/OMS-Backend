"""
Make a Jivo user the all-access OMS user.

Access model (see OMS-Frontend src/components/Sidebar.tsx): a user with the
`admin` role sees every page — `canSee = isAdmin || extra_pages.includes(key)`.
So this command assigns the `admin` role AND fills `extra_pages` with every
grantable page key (belt-and-suspenders), and flags Django is_staff/is_superuser.

The person must already exist in Jivo Auth (auth.jivo.in) with access to
OMS: accounts and passwords live there, and this command never sets one. It
finds them by email with the application's API key, gives them an OMS row if
they have none (or links the unlinked row with that email), and makes it the
admin. See docs/jivo-auth-integration.md.

Idempotent: re-running updates the existing user.

    python manage.py create_super_user --email someone@jivo.in
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jivo_auth.users import get_local_user

from users.jivo import JivoUnavailable, jivo_directory
from users.models import UserRole

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
    "Einvoice",
    "Ewaybill",
]


class Command(BaseCommand):
    help = "Give a Jivo user with OMS access the admin role and all page permissions."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True,
                            help="The Jivo Auth email of the person to make admin.")

    @transaction.atomic
    def handle(self, *args, **opts):
        email = opts["email"].strip().lower()
        try:
            jivo = next(
                (u for u in jivo_directory() if u["email"].lower() == email), None)
        except JivoUnavailable as exc:
            raise CommandError(f"Jivo Auth: {exc.detail}") from exc
        if jivo is None:
            raise CommandError(
                f"No Jivo user with access to OMS has the email {email}. Create "
                "them in Jivo Auth and grant OMS first.")

        role, role_created = UserRole.objects.get_or_create(
            name="admin",
            defaults={"display_name": "Admin", "is_active": True},
        )
        if not role.is_active:
            role.is_active = True
            role.save(update_fields=["is_active"])

        created = jivo["oms_user_id"] is None
        user = get_local_user({"sub": jivo["auth_id"], "email": jivo["email"]})
        user.name = user.name or jivo["name"] or jivo["email"]
        user.role = role
        user.extra_pages = list(ALL_PAGE_KEYS)
        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.save()

        self.stdout.write(self.style.SUCCESS(
            f"{'Created' if created else 'Updated'} OMS user {user.pk} for {jivo['email']} "
            f"(role=admin{' [role created]' if role_created else ''})."
        ))
        self.stdout.write(f"  extra_pages: {user.extra_pages}")
        self.stdout.write(self.style.WARNING(
            "  Sign in with the Jivo Auth email and password."))
