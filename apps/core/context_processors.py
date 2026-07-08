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
    if user is not None and user.is_authenticated:
        user_initials = _initials(user.full_name or user.email, "U")

    tenant = getattr(request, "tenant", None)
    tenant_name = ""
    tenant_initials = ""
    if tenant is not None and tenant.schema_name != get_public_schema_name():
        tenant_name = tenant.name
        tenant_initials = _initials(tenant.name, "H")

    return {
        "ui_user_initials": user_initials,
        "ui_tenant_name": tenant_name,
        "ui_tenant_initials": tenant_initials,
    }
