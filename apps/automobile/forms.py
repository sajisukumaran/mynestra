"""Automobile forms. Light ModelForms for the free-text / choice / date identity fields; ownership,
currency, parties, terms, drivers, funding, cost events and service-invoice job/part lines are
hand-rendered and parsed in the view (matching the app's htmx idiom, mirroring Loans). The record
forms bind the *structured* fields only — the lienholder/station/tax-authority org and the
fee/vendor/funding block stay hand-parsed in the view."""

from django import forms

from apps.automobile.models import (
    Vehicle,
    VehicleInspection,
    VehiclePropertyTax,
    VehicleRegistration,
)


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = [
            "nickname", "year", "make", "model_name", "trim", "body_type", "color",
            "vin", "license_plate", "plate_jurisdiction", "title_number",
            "warranty_provider", "notes",
        ]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. Family SUV"}),
            "make": forms.TextInput(attrs={"placeholder": "e.g. Toyota"}),
            "model_name": forms.TextInput(attrs={"placeholder": "e.g. Highlander"}),
        }


class RegistrationForm(forms.ModelForm):
    """Structured fields of a registration term (dates/choices/text). Bound to hand-rendered inputs;
    the lienholder org + fee/vendor/funding block are hand-parsed in the view."""

    class Meta:
        model = VehicleRegistration
        fields = [
            "jurisdiction", "plate_number", "plate_type", "title_number",
            "title_jurisdiction", "title_status", "effective_from", "expires_on", "reason", "note",
        ]


class InspectionForm(forms.ModelForm):
    class Meta:
        model = VehicleInspection
        fields = [
            "kind", "performed_on", "result", "expires_on", "certificate_number",
            "odometer", "note",
        ]


class PropertyTaxForm(forms.ModelForm):
    class Meta:
        model = VehiclePropertyTax
        fields = [
            "tax_year", "jurisdiction", "assessed_value", "rate", "amount",
            "assessed_on", "due_date", "note",
        ]
