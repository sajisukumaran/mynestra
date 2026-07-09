from django.contrib import admin

from .models import Branch, Organization, OrgIdentifier


class OrgIdentifierInline(admin.TabularInline):
    model = OrgIdentifier
    extra = 0


class BranchInline(admin.TabularInline):
    model = Branch
    extra = 0


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "display_name", "website")
    search_fields = ("name", "display_name")
    inlines = [OrgIdentifierInline, BranchInline]
