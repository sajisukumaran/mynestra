"""Public-schema URLs (served for every path NOT under /t/<slug>/)."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from apps.accounts import views as account_views
from apps.core.views import health, styleguide

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("styleguide/", styleguide, name="styleguide"),
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
    # Per-user theme persistence (topbar toggle); public so it works on any page.
    path("theme/", account_views.set_theme, name="set-theme"),
    # Invitation accept — un-prefixed, single-use token
    path("invite/<str:token>/", account_views.invite_accept, name="invite-accept"),
    # Tenant chooser / landing when authenticated
    path("", account_views.chooser, name="chooser"),
]

# Serve uploaded media (e.g. tenant logos) via Django in dev; nginx proxies /media/ to web.
# In production this is served by the web server / object storage instead.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Match the tenant urlconf: define error handlers so 404/500 render under DEBUG=False. Default
# views for now; on-brand styled pages land in P7.
handler400 = "django.views.defaults.bad_request"
handler403 = "django.views.defaults.permission_denied"
handler404 = "django.views.defaults.page_not_found"
handler500 = "django.views.defaults.server_error"
