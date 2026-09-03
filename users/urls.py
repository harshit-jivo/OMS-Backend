from django.urls import path
from .views import DeleteUserView,LoginView,LogoutView,AuthTokenRefreshView,PartyUsersView,AssignPartiesView, BulkAssignUsersPartiesView, ProfileView,StateListView, UserDetailView,UserPartiesView,UpdateProductRateView,RemoveProductFromPartyView,RemovePartyAssignmentView,PartyProductsView,AssignProductToPartyView,BulkAssignProductsToPartyView,UserListForAssignmentView,CompanyListView,MainGroupListView,CreateUserView,RoleListView,BulkAssignPartyToProductView,CategoryListView,PagePermissionsView,ComboMappingsView,PermissionRegistryView,RolePermissionsListView,RolePermissionsUpdateView,RoleCreateView,RoleUpdateView,RoleDeleteView
from django.views.decorators.csrf import csrf_exempt


urlpatterns = [

    path('login/', LoginView.as_view(), name='login'),
    path('refresh/', AuthTokenRefreshView.as_view(), name='token-refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', ProfileView.as_view(), name='profile'),

    # Master data APIS
    path('states/',StateListView.as_view(),name='states'),
    path('companies/',CompanyListView.as_view(),name='companies'),
    path('mainGroup/', MainGroupListView.as_view(), name='mainGroup'),
    path('categories/', CategoryListView.as_view(), name='categories'),
    path('users/create/', CreateUserView.as_view(), name='create-user'),
    path('roles/', RoleListView.as_view(), name='roles-list'),
    path('users/list/', UserListForAssignmentView.as_view(), name='users-list'),
    path('users/<int:user_id>/page-permissions/', PagePermissionsView.as_view(), name='user-page-permissions'),

    # Role Permissions matrix (Phase 4): the registry, and per-role bundles.
    path('permission-registry/', PermissionRegistryView.as_view(), name='permission-registry'),
    path('roles/permissions/', RolePermissionsListView.as_view(), name='role-permissions-list'),
    path('roles/<int:role_id>/permissions/', RolePermissionsUpdateView.as_view(), name='role-permissions-update'),
    path('roles/create/', RoleCreateView.as_view(), name='role-create'),
    path('roles/<int:role_id>/update/', RoleUpdateView.as_view(), name='role-update'),
    path('roles/<int:role_id>/delete/', RoleDeleteView.as_view(), name='role-delete'),

    # User-Party assignment
    path('users/<int:user_id>/parties/', UserPartiesView.as_view(), name='user-parties'),
    path('parties/<str:card_code>/users/', PartyUsersView.as_view(), name='party-users'),
    path('assign-parties/', AssignPartiesView.as_view(), name='assign-parties'),
    path('assign-parties/bulk-upload/', BulkAssignUsersPartiesView.as_view(), name='bulk-assign-users-parties'),
    path('remove-party/', RemovePartyAssignmentView.as_view(), name='remove-party'),

    # Party-Product assignment (with basic_rate)
    path('parties/<str:card_code>/products/', PartyProductsView.as_view(), name='party-products'),
    path('party-product/add/', AssignProductToPartyView.as_view(), name='add-product-to-party'),
    path('party-product/bulk-add/', BulkAssignProductsToPartyView.as_view(), name='bulk-add-products-to-party'),
    path('party-product/update-rate/', UpdateProductRateView.as_view(), name='update-product-rate'),
    path('party-product/remove/', RemoveProductFromPartyView.as_view(), name='remove-product-from-party'),

    # Combo pack -> free-of-cost item mapping (one entry per combo, applied to
    # every party that has the combo assigned).
    path('combo-mappings/', ComboMappingsView.as_view(), name='combo-mappings'),

    path('users/<int:user_id>/', UserDetailView.as_view(), name='user-detail'),
    path('users/<int:user_id>/delete/', DeleteUserView.as_view(), name='delete-user'),
    path('bulk-party/assign-products/', BulkAssignPartyToProductView.as_view())
    
]
