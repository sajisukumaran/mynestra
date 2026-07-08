from django.contrib import admin

from .models import PersonOrgRelationshipType, RelationshipType


@admin.register(RelationshipType)
class RelationshipTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "is_symmetric", "a_label_n", "b_label_n", "is_system")
    list_filter = ("is_symmetric", "is_system")
    search_fields = ("code",)


@admin.register(PersonOrgRelationshipType)
class PersonOrgRelationshipTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "label", "is_system")
    list_filter = ("is_system",)
    search_fields = ("code", "label")
