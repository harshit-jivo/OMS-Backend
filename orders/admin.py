from django.contrib import admin
from .models import StaffProductPrice, OrderFlowConfig

# @admin.register(OrderLog)
admin.site.register(StaffProductPrice)
admin.site.register(OrderFlowConfig)
