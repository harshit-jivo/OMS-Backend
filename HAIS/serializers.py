"""DRF serializers for HAIS.

The dropdown masters are plain CRUD. The Asset serializer carries the business
rules that mirror the frontend service:
  • Serial Number is required + unique; the QR is generated from it.
  • Asset ID is system-generated per category (AssetType.id_prefix) when omitted.
  • Storage Type is multi-select (write ids, read names).
  • Creating an asset seeds the first log row — "Assigned" when it has a holder,
    otherwise "Added to Store" for an unassigned (in-stock) device.
"""
from django.db import transaction
from rest_framework import serializers

from .models import Asset, AssetLog, AssetStorageType, AssetType, Department, StorageType


# --- dropdown masters -------------------------------------------------------
class AssetTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetType
        fields = ["id", "name", "id_prefix", "sort_order", "is_active"]


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "name", "sort_order", "is_active"]


class StorageTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = StorageType
        fields = ["id", "name", "sort_order", "is_active"]


# --- log --------------------------------------------------------------------
class AssetLogSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(
        source="department.name", read_only=True, default=None
    )

    class Meta:
        model = AssetLog
        fields = [
            "id", "action", "event_date",
            "from_user_id", "from_user_name", "to_user_id", "to_user_name",
            "department", "department_name", "location", "reason",
            "config_json", "config_change", "created_at", "created_by",
        ]
        read_only_fields = ["id", "created_at"]


# --- asset ------------------------------------------------------------------
CONFIG_FIELDS = ["processor", "memory", "operating_system", "storage"]


class AssetSerializer(serializers.ModelSerializer):
    asset_type_name = serializers.CharField(
        source="asset_type.name", read_only=True, default=None
    )
    department_name = serializers.CharField(
        source="department.name", read_only=True, default=None
    )
    # Multi-select storage: accept ids on write, expose names on read.
    storage_type_ids = serializers.PrimaryKeyRelatedField(
        source="storage_types", queryset=StorageType.objects.all(),
        many=True, required=False, write_only=True,
    )
    storage_type_names = serializers.SerializerMethodField(read_only=True)
    logs = AssetLogSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = [
            "asset_id", "serial_num", "qr_code",
            "asset_type", "asset_type_name", "company", "model_num", "warranty_ends",
            "processor", "memory", "operating_system", "storage",
            "storage_type_ids", "storage_type_names",
            "current_user_id", "current_user_name", "prev_user_id", "prev_user_name",
            "department", "department_name", "email_id", "current_location", "handover_date",
            "purchase_invoice_no", "purchase_invoice_date", "amount", "vendor",
            "date_of_last_service", "working_status", "remarks",
            "logs", "created_at", "updated_at",
        ]
        # asset_id is optional on create (system-generated); qr_code is derived.
        read_only_fields = ["qr_code", "created_at", "updated_at"]
        extra_kwargs = {"asset_id": {"required": False}}

    def get_storage_type_names(self, obj):
        return [s.name for s in obj.storage_types.all()]

    # --- helpers ---
    @staticmethod
    def _next_asset_id(asset_type):
        prefix = (getattr(asset_type, "id_prefix", "") or "AS").strip() or "AS"
        base = f"JIVO-{prefix}-"
        highest = 0
        for aid in Asset.objects.filter(asset_id__startswith=base).values_list(
            "asset_id", flat=True
        ):
            try:
                highest = max(highest, int(aid[len(base):]))
            except (ValueError, TypeError):
                continue
        return f"{base}{highest + 1:04d}"

    @staticmethod
    def _config_snapshot(asset, storage_types):
        snap = {f: getattr(asset, f) for f in CONFIG_FIELDS}
        snap["storage_types"] = [s.name for s in storage_types]
        return snap

    # --- create ---
    @transaction.atomic
    def create(self, validated_data):
        storage_types = validated_data.pop("storage_types", [])
        serial = (validated_data.get("serial_num") or "").strip()
        validated_data["serial_num"] = serial
        validated_data["qr_code"] = serial  # QR encodes the unique serial

        if not (validated_data.get("asset_id") or "").strip():
            validated_data["asset_id"] = self._next_asset_id(
                validated_data.get("asset_type")
            )

        asset = Asset.objects.create(**validated_data)
        for st in storage_types:
            AssetStorageType.objects.create(asset=asset, storage_type=st)

        has_holder = bool(
            (asset.current_user_name or "").strip() or (asset.current_user_id or "").strip()
        )
        AssetLog.objects.create(
            asset=asset,
            action=AssetLog.ASSIGNED if has_holder else AssetLog.ADDED_TO_STORE,
            event_date=asset.handover_date,
            to_user_id=asset.current_user_id,
            to_user_name=asset.current_user_name,
            department=asset.department,
            location=asset.current_location,
            reason="Initial assignment" if has_holder else "Received — not yet assigned",
            config_json=self._config_snapshot(asset, storage_types),
            created_by=self._actor(),
        )
        return asset

    # --- update ---
    @transaction.atomic
    def update(self, instance, validated_data):
        storage_types = validated_data.pop("storage_types", None)
        if "serial_num" in validated_data:
            serial = (validated_data["serial_num"] or "").strip()
            validated_data["serial_num"] = serial
            validated_data["qr_code"] = serial

        before_holder = instance.current_user_id
        before_config = self._config_snapshot(
            instance, list(instance.storage_types.all())
        )

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if storage_types is not None:
            instance.storage_links.all().delete()
            for st in storage_types:
                AssetStorageType.objects.create(asset=instance, storage_type=st)

        after_types = list(instance.storage_types.all())
        after_config = self._config_snapshot(instance, after_types)
        user_changed = "current_user_id" in validated_data and str(
            validated_data["current_user_id"]
        ) != str(before_holder)
        config_changed = before_config != after_config

        if user_changed or config_changed:
            AssetLog.objects.create(
                asset=instance,
                action=AssetLog.HANDOVER if user_changed else AssetLog.CONFIG_UPDATED,
                event_date=instance.handover_date,
                from_user_id=before_holder if user_changed else "",
                from_user_name=instance.prev_user_name if user_changed else "",
                to_user_id=instance.current_user_id,
                to_user_name=instance.current_user_name,
                department=instance.department,
                location=instance.current_location,
                reason=self.context.get("reason", "")
                or ("Reassigned" if user_changed else "Configuration updated"),
                config_json=after_config,
                created_by=self._actor(),
            )
        return instance

    def _actor(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            return getattr(user, "username", "") or str(user)
        return ""


class PublicAssetSerializer(serializers.ModelSerializer):
    """What an ANONYMOUS scanner sees.

    A QR sticker on a laptop is readable by anyone who picks the laptop up, so
    this endpoint has no session behind it and the field list is the whole
    security control. It is an allow-list, deliberately: adding a field to
    `AssetSerializer` must never widen what the public page leaks, and a
    `fields = "__all__"` here would do exactly that the next time the model
    grows a column.

    WHAT IS DELIBERATELY NOT HERE
    -----------------------------
    * `current_user_id` / `prev_user_id` — employee numbers, and the previous
      holder is nobody's business at a sticker;
    * `purchase_invoice_no`, `purchase_invoice_date`, `amount`, `vendor` —
      what the company paid and who it bought from;
    * `logs` — the full handover history: who had this machine, when, and why
      it moved. That is a movement record of named staff.

    What IS here is the answer to "whose is this and how do I return it":
    the device, its holder, and a way to reach them.
    """

    asset_type_name = serializers.CharField(source="asset_type.name", default="", read_only=True)
    department_name = serializers.CharField(source="department.name", default="", read_only=True)
    storage_type_names = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = [
            # Identity of the DEVICE.
            "asset_id", "serial_num",
            "asset_type_name", "company", "model_num", "working_status",
            # Configuration, so a scanner can confirm it is the right machine.
            "processor", "memory", "operating_system", "storage",
            "storage_type_names", "warranty_ends",
            # Whose it is, and how to return it.
            "current_user_name", "department_name", "email_id", "current_location",
        ]
        read_only_fields = fields

    def get_storage_type_names(self, obj):
        return [s.name for s in obj.storage_types.all()]
