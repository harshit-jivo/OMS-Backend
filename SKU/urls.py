from django.urls import path
from .views import SKUListView, SKUCreateView , SKUDetailView , SKUPendingList

urlpatterns = [
    path('upload/', SKUCreateView.as_view(), name='sku-upload'),
    path('all/', SKUListView.as_view(), name='sku-list'),
    path('pending/', SKUPendingList.as_view(), name='sku-pending'),
    path('<str:item_code>/', SKUDetailView.as_view(), name='sku-detail'),
]