"""Public-schema URLs (served for every path NOT under /t/<slug>/)."""

import re

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, re_path
from django.views.static import serve as serve_media

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

# Serve uploaded media (tenant logos, person/family photos) via Django. In dev the app-local nginx
# proxies /media/ here; in prod the edge proxy forwards /media/ here too (WhiteNoise serves only
# /static/), so prod opts in via SERVE_MEDIA. object storage would replace this at larger scale.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif getattr(settings, "SERVE_MEDIA", False):
    # static() no-ops when DEBUG is False, so route /media/ straight at the serve view in prod.
    _media = re.escape(settings.MEDIA_URL.lstrip("/"))
    urlpatterns += [
        re_path(rf"^{_media}(?P<path>.*)$", serve_media, {"document_root": settings.MEDIA_ROOT}),
    ]

# Match the tenant urlconf: define error handlers so 404/500 render under DEBUG=False. The default
# views render our on-brand templates/{400,403,404,500}.html (P7).
handler400 = "django.views.defaults.bad_request"
handler403 = "django.views.defaults.permission_denied"
handler404 = "django.views.defaults.page_not_found"
handler500 = "django.views.defaults.server_error"
