"""Public-schema URLs (served for every path NOT under /t/<slug>/)."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from apps.accounts import views as account_views
from apps.core.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    # Auth (public identity schema)
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password-reset/", auth_views.PasswordResetView.as_view(), name="password_reset"),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    # Invitation accept — un-prefixed, single-use token
    path("invite/<str:token>/", account_views.invite_accept, name="invite-accept"),
    # Tenant chooser / landing when authenticated
    path("", account_views.chooser, name="chooser"),
]
