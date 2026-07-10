from django.urls import path

from . import views as v

urlpatterns = [
    path("token/", v.ewb_token, name="ewb-token"),
    path("generate/", v.generate_ewb, name="ewb-generate"),
    path("from-invoice/<int:docentry>/", v.ewb_from_invoice, name="ewb-from-invoice"),
    path("update-part-b/", v.update_part_b, name="ewb-update-part-b"),
    path("cancel/", v.cancel_ewb, name="ewb-cancel"),
    path("close/", v.close_ewb, name="ewb-close"),
    path("reject/", v.reject_ewb, name="ewb-reject"),
    path("extend-validity/", v.extend_validity, name="ewb-extend"),
    path("update-transporter/", v.update_transporter, name="ewb-update-transporter"),
    path("gstin/<str:gstin>/", v.ewb_gstin_details, name="ewb-gstin"),
    path("transporter/<str:trans_id>/", v.ewb_transporter_details, name="ewb-transporter"),
    path("<str:ewb_no>/", v.get_ewb, name="ewb-get"),
]
