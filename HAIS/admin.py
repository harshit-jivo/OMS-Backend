from django.contrib import admin

from .models import Asset, AssetLog, AssetStorageType, AssetType, Department, StorageType


@admin.register(AssetType)
class AssetTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "id_prefix", "sort_order", "is_active")
    list_editable = ("id_prefix", "sort_order", "is_active")
    search_fields = ("name",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name",)


@admin.register(StorageType)
class StorageTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name",)


class AssetStorageTypeInline(admin.TabularInline):
    model = AssetStorageType
    extra = 1


class AssetLogInline(admin.TabularInline):
    model = AssetLog
    extra = 0
    can_delete = False
    readonly_fields = ("action", "event_date", "from_user_name", "to_user_name",
                       "reason", "config_change", "created_at")


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("asset_id", "serial_num", "asset_type", "company", "model_num",
                    "current_user_name", "department", "working_status")
    list_filter = ("working_status", "asset_type", "department")
    search_fields = ("asset_id", "serial_num", "current_user_name", "current_user_id",
                     "company", "model_num")
    inlines = [AssetStorageTypeInline, AssetLogInline]


@admin.register(AssetLog)
class AssetLogAdmin(admin.ModelAdmin):
    list_display = ("asset", "action", "event_date", "from_user_name",
                    "to_user_name", "reason", "created_at")
    list_filter = ("action",)
    search_fields = ("asset__asset_id", "from_user_name", "to_user_name")
