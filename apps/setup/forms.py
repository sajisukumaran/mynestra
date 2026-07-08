"""Setup forms. Used for validation + error rendering; templates render the inputs with cotton
components bound to these fields' values."""

from django import forms

from apps.setup.models import Category

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
