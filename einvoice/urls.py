from django.urls import path

from . import views

urlpatterns = [
    # config + auth
    path("health/", views.health, name="einvoice-health"),
    path("token/", views.get_token, name="einvoice-get-token"),
    path("heartbeat/", views.heartbeat, name="einvoice-heartbeat"),

    # IRN
    path("irn/", views.generate_irn, name="einvoice-generate-irn"),
    path("irn/validate/", views.validate_irn, name="einvoice-validate-irn"),
    path("irn/from-invoice/<int:docentry>/", views.irn_from_invoice, name="einvoice-irn-from-invoice"),
    path("irn/sample/", views.generate_irn_sample, name="einvoice-generate-irn-sample"),
    path("invoices/", views.list_invoices, name="einvoice-list-invoices"),
    path("logs/", views.generation_logs, name="einvoice-generation-logs"),
    path("logs/retry/", views.retry_generation, name="einvoice-retry-generation"),
    path("irn/cancel/", views.cancel_irn, name="einvoice-cancel-irn"),
    path("irn/by-doc/", views.get_irn_by_doc, name="einvoice-irn-by-doc"),
    path("irn/rejected/", views.get_rejected_irns, name="einvoice-rejected-irns"),
    path("qr/", views.render_qr, name="einvoice-render-qr"),
    path("irn/<str:irn>/qr.png", views.irn_qr_png, name="einvoice-irn-qr-png"),
    path("irn/<str:irn>/", views.get_irn_details, name="einvoice-irn-details"),

    # e-Way Bill (by IRN)
    path("ewb/", views.generate_ewb_by_irn, name="einvoice-generate-ewb"),
    path("ewb/<str:irn>/", views.get_ewb_by_irn, name="einvoice-ewb-by-irn"),

    # GSTIN master
    path("gstin/<str:gstin>/", views.get_gstin_details, name="einvoice-gstin-details"),
    path("gstin/<str:gstin>/sync/", views.sync_gstin, name="einvoice-sync-gstin"),
]
