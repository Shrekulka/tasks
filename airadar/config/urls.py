# airadar/config/urls.py

from django.urls import include, path

urlpatterns = [
    path("", include("radar.urls")),
]

handler404 = "radar.views.page_not_found"
handler500 = "radar.views.server_error"