from django.contrib import admin

from .models import Domain, Invitation, Membership, Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "schema_name", "palette", "created_on")
    search_fields = ("name", "schema_name")


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "tenant", "is_primary")
    search_fields = ("domain",)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "joined_at")
    list_filter = ("role",)
    search_fields = ("user__email", "tenant__name")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "tenant", "role", "status", "expires_at", "created_at")
    list_filter = ("status", "role")
    search_fields = ("email", "tenant__name")
    readonly_fields = ("token", "created_at")
