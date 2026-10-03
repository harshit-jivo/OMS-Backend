"""Control Panel routes. Mounted at ``api/control-panel/`` (and ``api/v1/...``).

  POST /sso/   {"page": <id from permissions.PAGES>} -> the embedded page's sign-on link
"""
from django.urls import path

from control_panel import views

urlpatterns = [
    path('sso/', views.SsoLinkView.as_view(), name='control-panel-sso'),
]
