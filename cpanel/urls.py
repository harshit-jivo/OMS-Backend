"""The Control Panel's routes inside OMS.

C_Panel's pages and APIs keep their ORIGINAL paths and names (/realise/,
/sales/, /inventory/, /expenses/, /salaries/, /api/expenses/, /api/nav-ticker/
...). The page scripts address their APIs by literal path — about seven
hundred of them — so moving the pages under a prefix would mean editing those
scripts, and the point is that the pages are production's, unedited. These
patterns are appended AFTER OMS's own in OMS/urls.py, so an OMS route of the
same path would always win; none collides today.

Every page and API checks access itself (cpanel/core/decorators.py), from the
user's OMS keys (cpanel/core/oms_access.py). Routes for C_Panel pages OMS does
not show still exist — templates link to them by name — but every such page's
flag is False, so they answer 403.

OMS-only routes live under /cp/: the sign-in handshake, the signed-out page,
and placeholders for C_Panel's login, logout, look switcher and user admin.
"""
from django.urls import include, path

from cpanel.core import views as core_views
from cpanel.home import views as home_views

urlpatterns = [
    # Sign-in from the OMS app (single-use ticket -> session for the same user).
    path('cp/session/', core_views.session_from_oms, name='cp_session'),
    path('cp/signed-out/', core_views.signed_out, name='signed_out'),
    # C_Panel's landing: the first page this user can open.
    path('cp/home/', home_views.landing, name='home'),
    # C_Panel routes OMS replaces with its own (see core/views.placeholder).
    path('cp/login/', core_views.placeholder, name='login'),
    path('cp/logout/', core_views.placeholder, name='logout'),
    path('cp/ui/switch/', core_views.placeholder, name='switch_ui'),
    path('cp/users/', core_views.placeholder, name='user_management'),
    path('cp/api/users/save/', core_views.placeholder, name='api_user_save'),
    path('cp/api/users/delete/', core_views.placeholder, name='api_user_delete'),

    # C_Panel's own routes, at their original paths.
    path('api/nav-ticker/', core_views.nav_ticker, name='nav_ticker'),
    path('realise/', include('cpanel.realise.urls')),
    path('sales/', include('cpanel.sales.urls')),
    path('inventory/', include('cpanel.inventory.urls')),
    path('', include('cpanel.dashboard.urls')),
]
