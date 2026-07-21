"""Identity views (public + tenant). Login/logout/password-reset use Django's built-in views
(wired in config/urls_public.py). Invitation *management* moved to the Setup app in P3
(apps/setup); this module keeps the chooser, tenant landing, and the public accept flow."""

from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.tenants.models import Invitation, Membership, Role
from apps.users.models import User


def set_theme(request):
    """Persist the caller's per-user theme (DESIGN §7.2). Called by the topbar toggle via fetch;
    public so it works on any page. No-op for anonymous callers. Returns 204."""
    if request.method == "POST" and request.user.is_authenticated:
        value = request.POST.get("theme", "")
        request.user.theme = value if value in User.Theme.values else None
        request.user.save(update_fields=["theme"])
    return HttpResponse(status=204)


@login_required
def chooser(request):
    """Public landing: pick a household. Redirect straight through when there's an obvious one."""
    memberships = list(request.user.memberships.select_related("tenant").all())

    default_tenant = request.user.default_tenant
    if default_tenant and any(m.tenant_id == default_tenant.id for m in memberships):
        return redirect(f"/t/{default_tenant.slug}/")
    if len(memberships) == 1:
        return redirect(f"/t/{memberships[0].tenant.slug}/")

    return render(request, "accounts/chooser.html", {"memberships": memberships})


def tenant_home(request):
    """Launcher (DESIGN §7.4/§9): a live infolet per enabled module + muted 'coming soon' tiles.
    Counts come from each module's AppConfig and run in the current tenant schema."""
    from apps.core.registry import COMING_SOON, enabled_modules
    from apps.finance.services import base_currency

    is_owner = Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()
    modules = [
        {"meta": cfg.launcher_module, "counts": cfg.launcher_counts()}
        for cfg in enabled_modules(request.tenant)
    ]
    return render(
        request,
        "accounts/tenant_home.html",
        {
            "tenant": request.tenant,
            "is_owner": is_owner,
            "modules": modules,
            "coming_soon": COMING_SOON,
            # Symbol for money-valued launcher stats (e.g. Investments "Value"); the tile formats
            # them via c-money so a real balance stays grouped + compact instead of overflowing.
            "base_symbol": base_currency().symbol,
        },
    )


def invite_accept(request, token):
    """Public accept flow: new user sets name+password then joins; existing user just joins."""
    invitation = get_object_or_404(Invitation, token=token)
    if not invitation.is_actionable:
        return render(
            request, "accounts/invite_invalid.html", {"invitation": invitation}, status=410
        )

    User = get_user_model()
    existing = User.objects.filter(email=invitation.email.lower()).first()

    if request.method == "POST":
        errors = []
        if existing:
            user = existing
        else:
            full_name = request.POST.get("full_name", "").strip()
            password = request.POST.get("password", "")
            if len(password) < 8:
                errors.append("Password must be at least 8 characters.")
            if not errors:
                user = User.objects.create_user(
                    email=invitation.email, password=password, full_name=full_name
                )
        if errors:
            return render(
                request,
                "accounts/invite_accept.html",
                {"invitation": invitation, "existing": bool(existing), "errors": errors},
            )

        Membership.objects.get_or_create(
            user=user, tenant=invitation.tenant, defaults={"role": invitation.role}
        )
        invitation.status = Invitation.Status.ACCEPTED
        invitation.save(update_fields=["status"])

        landing = f"/t/{invitation.tenant.slug}/"
        if existing:
            # Existing account: send them through login (their own password), landing on the tenant.
            return redirect_to_login(landing)
        login(request, user)
        return redirect(landing)

    return render(
        request,
        "accounts/invite_accept.html",
        {"invitation": invitation, "existing": bool(existing)},
    )
