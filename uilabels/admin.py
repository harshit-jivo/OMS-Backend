from django.contrib import admin

from .models import UILabel


@admin.register(UILabel)
class UILabelAdmin(admin.ModelAdmin):
    list_display = (
        'field_key', 'display_name', 'is_active',
        'is_enabled', 'is_required', 'updated_at',
    )
    list_filter = ('is_active', 'is_enabled', 'is_required')
    search_fields = ('field_key', 'display_name', 'description')
    readonly_fields = ('created_at', 'updated_at')
