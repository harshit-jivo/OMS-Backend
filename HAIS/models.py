"""HAIS — Hardware Asset Identification Software: persistence layer.

Six tables, all living in a dedicated Postgres schema ``hais`` (created by the
initial migration). The schema-qualified table names use Django's documented
``'schema"."table'`` db_table form, which Postgres reads as ``"hais"."tbl_X"``.

Layout:
  • three DYNAMIC dropdown masters — AssetType / Department / StorageType.
    Only Working Status is static (a code list on the asset), everything else a
    dropdown is served from these tables.
  • Asset — the device, keyed by its Asset ID (the current state).
  • AssetStorageType — the many-to-many link, because a device can have more
    than one storage type (e.g. SSD + HDD).
  • AssetLog — one row per movement/event: who → who, when, why, and a snapshot
    of the configuration at that moment. The whole person-wise history lives here.

Everything hangs off the Asset ID: the asset row is "now", the log rows are the
full story. User details are stored INLINE (id + name) on the asset and copied
onto each log row, so history is a permanent snapshot with no person master.
"""
from django.db import models


def _t(table: str) -> str:
    """Schema-qualify a table name for the ``hais`` Postgres schema."""
    return f'hais"."{table}'


# ---------------------------------------------------------------------------
# Dynamic dropdown masters
# ---------------------------------------------------------------------------
class _Option(models.Model):
    """Shared shape for the dropdown master tables."""

    name = models.CharField(max_length=100, unique=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class AssetType(_Option):
    """Asset Type / Category, e.g. Laptop, Desktop, Monitor."""

    # Short code used when the system generates an Asset ID (LT, DT, MN, ...),
    # so adding a new category is fully DB-driven — no code change.
    id_prefix = models.CharField(max_length=8, blank=True, default="")

    class Meta(_Option.Meta):
        db_table = _t("tbl_AssetType")
        verbose_name = "Asset Type"


class Department(_Option):
    """Owning department, e.g. IT, Accounts, Admin."""

    class Meta(_Option.Meta):
        db_table = _t("tbl_Departments")
        verbose_name = "Department"


class StorageType(_Option):
    """Storage medium, e.g. SSD, NVMe SSD, HDD. Multi-select on the device."""

    class Meta(_Option.Meta):
        db_table = _t("tbl_StorageType")
        verbose_name = "Storage Type"


# ---------------------------------------------------------------------------
# Asset — the device (current state), keyed by Asset ID
# ---------------------------------------------------------------------------
class Asset(models.Model):
    """One physical device. Its Asset ID is the primary key.

    Dates are kept as dd/mm/yyyy strings to match the shared frontend DateInput
    contract (the TS ``Asset`` type carries them as strings), so the service
    layer swap from the in-memory store is a drop-in.
    """

    # Working Status is the one STATIC list (not a dropdown master).
    WORKING = "Working"
    UNDER_REPAIR = "Under Repair"
    NOT_WORKING = "Not Working"
    IN_STOCK = "In Stock"
    SCRAPPED = "Scrapped"
    WORKING_STATUS_CHOICES = (
        (WORKING, "Working"),
        (UNDER_REPAIR, "Under Repair"),
        (NOT_WORKING, "Not Working"),
        (IN_STOCK, "In Stock"),
        (SCRAPPED, "Scrapped"),
    )

    # --- identity ---
    asset_id = models.CharField(max_length=40, primary_key=True)
    serial_num = models.CharField(max_length=120, unique=True)
    qr_code = models.CharField(max_length=160, blank=True, default="")

    # --- specs ---
    asset_type = models.ForeignKey(
        AssetType, on_delete=models.PROTECT, null=True, blank=True, related_name="assets"
    )
    company = models.CharField(max_length=100, blank=True, default="")
    model_num = models.CharField(max_length=100, blank=True, default="")
    warranty_ends = models.CharField(max_length=20, blank=True, default="")
    processor = models.CharField(max_length=120, blank=True, default="")
    memory = models.CharField(max_length=60, blank=True, default="")
    operating_system = models.CharField(max_length=80, blank=True, default="")
    storage = models.CharField(max_length=60, blank=True, default="")
    storage_types = models.ManyToManyField(
        StorageType, through="AssetStorageType", related_name="assets", blank=True
    )

    # --- holder (inline; both blank = Unassigned) ---
    current_user_id = models.CharField(max_length=40, blank=True, default="")
    current_user_name = models.CharField(max_length=120, blank=True, default="")
    prev_user_id = models.CharField(max_length=40, blank=True, default="")
    prev_user_name = models.CharField(max_length=120, blank=True, default="")

    # --- assignment / tracking ---
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True, related_name="assets"
    )
    email_id = models.CharField(max_length=120, blank=True, default="")
    current_location = models.CharField(max_length=160, blank=True, default="")
    handover_date = models.CharField(max_length=20, blank=True, default="")

    # --- purchase ---
    purchase_invoice_no = models.CharField(max_length=60, blank=True, default="")
    purchase_invoice_date = models.CharField(max_length=20, blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    vendor = models.CharField(max_length=120, blank=True, default="")

    # --- maintenance ---
    date_of_last_service = models.CharField(max_length=20, blank=True, default="")
    working_status = models.CharField(
        max_length=20, choices=WORKING_STATUS_CHOICES, default=WORKING
    )
    remarks = models.TextField(blank=True, default="")

    # --- audit ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t("tbl_Asset")
        verbose_name = "Asset"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["serial_num"], name="hais_asset_serial_idx"),
            models.Index(fields=["current_user_id"], name="hais_asset_curuser_idx"),
            models.Index(fields=["working_status"], name="hais_asset_status_idx"),
        ]

    def __str__(self):
        return self.asset_id

    @property
    def is_unassigned(self) -> bool:
        return not (self.current_user_name.strip() or self.current_user_id.strip())


class AssetStorageType(models.Model):
    """Link row: one storage type on one device (the multi-select)."""

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="storage_links")
    storage_type = models.ForeignKey(
        StorageType, on_delete=models.PROTECT, related_name="asset_links"
    )

    class Meta:
        db_table = _t("tbl_AssetStorageType")
        verbose_name = "Asset Storage Type"
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "storage_type"], name="hais_asset_storagetype_uq"
            ),
        ]

    def __str__(self):
        return f"{self.asset_id}:{self.storage_type_id}"


# ---------------------------------------------------------------------------
# AssetLog — full movement / history trail
# ---------------------------------------------------------------------------
class AssetLog(models.Model):
    """One event in a device's life. Append-only; never updated after write."""

    ASSIGNED = "Assigned"
    ADDED_TO_STORE = "Added to Store"
    HANDOVER = "Handover"
    REASSIGNED = "Reassigned"
    CONFIG_UPDATED = "Config Updated"
    SENT_FOR_SERVICE = "Sent for Service"
    RETURNED = "Returned"
    STATUS_CHANGED = "Status Changed"
    SCRAPPED = "Scrapped"

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="logs")

    action = models.CharField(max_length=40)
    event_date = models.CharField(max_length=20, blank=True, default="")

    # who → who (inline snapshot; independent of any user master)
    from_user_id = models.CharField(max_length=40, blank=True, default="")
    from_user_name = models.CharField(max_length=120, blank=True, default="")
    to_user_id = models.CharField(max_length=40, blank=True, default="")
    to_user_name = models.CharField(max_length=120, blank=True, default="")

    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs"
    )
    location = models.CharField(max_length=160, blank=True, default="")
    reason = models.CharField(max_length=255, blank=True, default="")

    # Config as it stood at this event (incl. the list of storage types).
    config_json = models.JSONField(default=dict, blank=True)
    config_change = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        db_table = _t("tbl_AssetLog")
        verbose_name = "Asset Log"
        ordering = ["asset_id", "id"]  # oldest first within a device
        indexes = [
            models.Index(fields=["asset", "id"], name="hais_log_asset_idx"),
        ]

    def __str__(self):
        return f"{self.asset_id}:{self.action}@{self.event_date}"
