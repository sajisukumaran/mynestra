"""Setup forms. Used for validation + error rendering; templates render the inputs with cotton
components bound to these fields' values."""

from django import forms

from apps.relationships.models import PersonOrgRelationshipType, RelationshipType
from apps.setup.models import Category
from apps.tenants.models import Role

# Curated category chip tints (DESIGN §7.1). The picker (c-tint-select) offers exactly these.
CATEGORY_TINTS = [
    "teal", "blue", "violet", "amber", "rose",
    "emerald", "sky", "slate", "orange", "fuchsia",
]


class CategoryForm(forms.ModelForm):
    """Edit a category's name + tint. `kind` is fixed by the instance (set on create by the view),
    so uniqueness (kind, name) is validated against the correct kind."""

    class Meta:
        model = Category
        fields = ["name", "color"]

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean_color(self):
        color = self.cleaned_data["color"]
        if color not in CATEGORY_TINTS:
            raise forms.ValidationError("Choose one of the available tints.")
        return color


class RelationshipTypeForm(forms.ModelForm):
    """P2P type: code + symmetry + the six gender-aware side labels (DESIGN §5)."""

    class Meta:
        model = RelationshipType
        fields = [
            "code", "is_symmetric",
            "a_label_m", "a_label_f", "a_label_n",
            "b_label_m", "b_label_f", "b_label_n",
        ]

    def clean_code(self):
        return self.cleaned_data["code"].strip()


class PersonOrgRelationshipTypeForm(forms.ModelForm):
    """P2O type: code + a single label (DESIGN §5)."""

    class Meta:
        model = PersonOrgRelationshipType
        fields = ["code", "label"]

    def clean_code(self):
        return self.cleaned_data["code"].strip()

    def clean_label(self):
        return self.cleaned_data["label"].strip()


ROLE_CHOICES = [(Role.MEMBER, "Member"), (Role.OWNER, "Owner")]


class InviteForm(forms.Form):
    """Invite an email into the current household with a role (DESIGN §4)."""

    email = forms.EmailField()
    role = forms.ChoiceField(choices=ROLE_CHOICES, initial=Role.MEMBER)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()
