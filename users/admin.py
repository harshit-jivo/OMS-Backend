from django import forms
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from .models import User, Company, MainGroup, State,UserRole


class JivoAdminAuthenticationForm(AdminAuthenticationForm):
    """The admin sign-in, labelled for what it takes: the Jivo email.

    `JivoAuthBackend` checks the email and password with Jivo Auth; staff
    status and permissions stay on the OMS row.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Email'
        self.fields['username'].widget = forms.EmailInput(
            attrs={'autofocus': True, 'autocomplete': 'username'})


admin.site.login_form = JivoAdminAuthenticationForm

@admin.register(UserRole)
@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']


@admin.register(MainGroup)
class MainGroupAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'code', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'code']


class LinkedToJivoFilter(admin.SimpleListFilter):
    title = 'linked to Jivo'
    parameter_name = 'jivo'

    def lookups(self, request, model_admin):
        return [('yes', 'Yes'), ('no', 'No')]

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(auth_id__isnull=False)
        if self.value() == 'no':
            return queryset.filter(auth_id__isnull=True)
        return queryset


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """OMS's side of a user: roles, scope and staff flags.

    A plain ModelAdmin, not django.contrib.auth's UserAdmin: that one serves
    `<id>/password/`, which would set a local password even with no link to
    it, and an add form that creates users with one. Accounts, names, emails
    and passwords belong to Jivo Auth; add people there, then in OMS (App
    User page, or their first sign-in).
    """

    filter_horizontal = ['extra_roles']
    list_display = ['id', 'username', 'name', 'email', 'auth_id', 'is_active']
    list_filter = ['is_active', LinkedToJivoFilter]
    search_fields = ['username', 'name', 'email', 'phone', 'auth_id']
    ordering = ['-id']
    readonly_fields = ['auth_id', 'name', 'email']

    fieldsets = (
        (None, {'fields': ('username', 'auth_id')}),
        ('Personal Info', {
            'fields': ('name', 'email', 'phone'),
            'description': 'Name and email are managed in Jivo Auth.',
        }),
        ('Organization', {'fields': ('company', 'main_group', 'state')}),
        ('Roles', {
            'fields': ('role', 'extra_roles'),
            'description': (
                '<b>Role</b> is the primary role and drives page access. '
                '<b>Extra roles</b> are additional functions (Payment Approver, '
                'Deposit Creator, …) held on top of it — use these so a user '
                'gains a payment function without losing their primary role.'
            ),
        }),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser')}),
    )

    def has_add_permission(self, request):
        # People are created in Jivo Auth, not here.
        return False
