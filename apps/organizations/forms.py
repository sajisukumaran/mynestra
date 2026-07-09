"""Organization + Branch forms. Contact channels and identifiers are edited as Alpine-managed
inline arrays (parsed in the view, like the Person form's channels); addresses are edited on the
detail page via slide-over. Mirrors apps.contacts.forms."""

from django import forms

from apps.organizations.models import Branch, Organization


class OrganizationForm(forms.ModelForm):
    # assume_scheme="https" adopts Django 6.0's URLField default now, without the transitional
    # setting (which itself warns). Bare domains like "hdfc.example" normalise to https://.
    website = forms.URLField(required=False, assume_scheme="https")

    class Meta:
        model = Organization
        fields = ["name", "display_name", "logo", "website", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True


class BranchForm(forms.ModelForm):
    """Edited from the org detail via a popup. Opened/Closed PartialDates + the folded primary
    address are parsed in the view (like the P2O dates / Person channels)."""

    class Meta:
        model = Branch
        fields = ["name", "number", "is_primary"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
