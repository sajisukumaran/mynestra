"""Setup URLs — mounted under /t/<slug>/setup/ (see config/urls_tenants.py). Owner-only.

Reversing is not subfolder-aware under django-tenants, so templates build links from
`ui_tenant_url` (e.g. "/t/<slug>/setup/..."); these names exist for clarity/tests.
"""

from django.urls import path

from apps.setup import views

app_name = "setup"

urlpatterns = [
    path("", views.overview, name="overview"),
    # Categories
    path("categories/", views.categories, name="categories"),
    path("categories/new/<str:kind>/", views.category_create, name="category-create"),
    path("categories/<int:pk>/edit/", views.category_edit, name="category-edit"),
    path("categories/<int:pk>/delete/", views.category_delete, name="category-delete"),
    # Relationship types (kind = p2p | p2o)
    path("relationship-types/", views.relationship_types, name="relationship-types"),
    path("relationship-types/<str:kind>/new/", views.rel_type_create, name="rel-type-create"),
    path("relationship-types/<str:kind>/<int:pk>/edit/", views.rel_type_edit, name="rel-type-edit"),
    path(
        "relationship-types/<str:kind>/<int:pk>/delete/",
        views.rel_type_delete,
        name="rel-type-delete",
    ),
    # Members & invitations
    path("members/", views.members, name="members"),
    path("members/invite/", views.member_invite, name="member-invite"),
    path("members/<int:pk>/role/", views.member_role, name="member-role"),
    path("members/<int:pk>/remove/", views.member_remove, name="member-remove"),
    path("invitations/<int:pk>/revoke/", views.invitation_revoke, name="invitation-revoke"),
    path("invitations/<int:pk>/resend/", views.invitation_resend, name="invitation-resend"),
    # Appearance
    path("appearance/", views.appearance, name="appearance"),
    # Household profile
    path("profile/", views.profile, name="profile"),
]
