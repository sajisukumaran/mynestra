from django.contrib import admin

from .models import Category


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "color", "is_system")
    list_filter = ("kind", "is_system")
    search_fields = ("name",)
