"""Template context for the app shell (topbar initials + current tenant)."""

from django_tenants.utils import get_public_schema_name


def _initials(text: str, fallback: str = "?") -> str:
    parts = (text or "").split()
    if len(parts) >= 2:
        return (parts[0][:1] + parts[-1][:1]).upper()
    if parts:
        return parts[0][:2].upper()
    return fallback


def ui(request):
    user = getattr(request, "user", None)
    user_initials = "U"
    # Per-user theme (DESIGN §7.2): explicit light/dark wins; "" means inherit system default.
    user_theme = ""
    if user is not None and user.is_authenticated:
        user_initials = _initials(user.full_name or user.email, "U")
        user_theme = user.theme or ""

    tenant = getattr(request, "tenant", None)
    tenant_name = ""
    tenant_initials = ""
    tenant_url = ""
    # Household palette (DESIGN §7.2): server-authoritative so Appearance changes recolor the app
    # for every member. Empty on public pages so the /styleguide localStorage switcher still works.
    tenant_palette = ""
    # Localization (Setup → Localization): exposed to every template for money/date formatting.
    tenant_currency = ""
    tenant_timezone = ""
    tenant_date_format = ""
    tenant_number_format = ""
    # Accounting mode (Setup → Mode): drives whether the Finance/GL surface and per-account
    # Accounting Setup tabs are shown. Empty/Standard on public pages.
    tenant_accounting_mode = ""
    tenant_accounting_locked = False
    if tenant is not None and tenant.schema_name != get_public_schema_name():
        tenant_name = tenant.name
        tenant_initials = _initials(tenant.name, "H")
        # Tenant routes are addressed explicitly as /t/<slug>/... (reversing is not subfolder-aware
        # in django-tenants); templates build links from this base.
        tenant_url = f"/t/{tenant.schema_name}/"
        tenant_palette = getattr(tenant, "palette", "") or ""
        tenant_currency = getattr(tenant, "currency", "") or ""
        tenant_timezone = getattr(tenant, "timezone", "") or ""
        tenant_date_format = getattr(tenant, "date_format", "") or ""
        tenant_number_format = getattr(tenant, "number_format", "") or ""
        tenant_accounting_mode = getattr(tenant, "accounting_mode", "") or ""
        tenant_accounting_locked = bool(getattr(tenant, "accounting_locked", False))

    return {
        "ui_user_initials": user_initials,
        "ui_tenant_name": tenant_name,
        "ui_tenant_initials": tenant_initials,
        "ui_tenant_url": tenant_url,
        "ui_palette": tenant_palette,
        "ui_theme": user_theme,
        "ui_currency": tenant_currency,
        "ui_timezone": tenant_timezone,
        "ui_date_format": tenant_date_format,
        "ui_number_format": tenant_number_format,
        "ui_accounting_mode": tenant_accounting_mode,
        "ui_expert": tenant_accounting_mode == "expert",
        "ui_accounting_locked": tenant_accounting_locked,
    }
