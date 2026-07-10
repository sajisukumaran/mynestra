"""Cards URLs — mounted under /t/<slug>/cards/. Member-accessible. Filled out in C4."""

from django.urls import path

from apps.cards import views

app_name = "cards"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]
