from django.contrib import admin

from .models import (
    PersonOrgRelationship,
    PersonOrgRelationshipType,
    PersonRelationship,
    RelationshipType,
)


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


@admin.register(PersonRelationship)
class PersonRelationshipAdmin(admin.ModelAdmin):
    list_display = ("person_a", "person_b", "type")
    list_filter = ("type",)
    raw_id_fields = ("person_a", "person_b")


@admin.register(PersonOrgRelationship)
class PersonOrgRelationshipAdmin(admin.ModelAdmin):
    list_display = ("person", "organization", "type")
    list_filter = ("type",)
    raw_id_fields = ("person", "organization")
