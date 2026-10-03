# airadar/radar/urls.py

from django.urls import path, re_path

from . import views

app_name = "radar"

urlpatterns = [
    path("", views.index, name="index"),
    path("catalog/", views.catalog, name="catalog"),
    path("catalog/export.csv", views.export_csv, name="export"),
    re_path(r"^site/(?P<domain>[A-Za-z0-9.-]{1,253})/$", views.site_detail, name="site"),
    path("compare/", views.compare, name="compare"),
    path("compare/verdict/", views.verdict, name="verdict"),   # P2
    path("about/", views.about, name="about"),
]