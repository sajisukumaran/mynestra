from django.contrib import admin

from .models import Family, FamilyMembership


class FamilyMembershipInline(admin.TabularInline):
    model = FamilyMembership
    extra = 0
    raw_id_fields = ("person",)


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "member_count")
    search_fields = ("name",)
    inlines = [FamilyMembershipInline]
