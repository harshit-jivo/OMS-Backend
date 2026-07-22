from django.contrib import admin

from .models import UILabel


@admin.register(UILabel)
class UILabelAdmin(admin.ModelAdmin):
    list_display = ('field_key', 'display_name', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('field_key', 'display_name', 'description')
    readonly_fields = ('created_at', 'updated_at')
