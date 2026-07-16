"""Real Estate forms. A light ModelForm for the property's free-text identity / address fields;
type, use, ownership, currency, seller, owners, acquisition and cost events are hand-rendered and
parsed in the view (the app's htmx idiom, mirroring Automobile / Insurance)."""

from django import forms

from apps.realestate.models import Property


class PropertyForm(forms.ModelForm):
    class Meta:
        model = Property
        fields = [
            "nickname", "address_line1", "address_line2", "city", "state",
            "postal_code", "country", "notes",
        ]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. Family home"}),
            "address_line1": forms.TextInput(attrs={"placeholder": "123 Main St"}),
        }
