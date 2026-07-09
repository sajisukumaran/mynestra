"""Organization + Branch forms. Contact channels and identifiers are edited as Alpine-managed
inline arrays (parsed in the view, like the Person form's channels); addresses are edited on the
detail page via slide-over. Mirrors apps.contacts.forms."""

from django import forms

from apps.organizations.models import Branch, Organization


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "display_name", "logo", "website", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True


class BranchForm(forms.ModelForm):
    """Edited from the org detail via a slide-over."""

    class Meta:
        model = Branch
        fields = ["name", "is_primary"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
