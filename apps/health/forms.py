"""Health forms. Light ModelForms for the free-text identity fields; type / status / parties /
dates / charges / roster / funding are hand-rendered and parsed in the view (the app's htmx idiom,
mirroring Insurance / Real Estate)."""

from django import forms

from apps.health.models import Encounter, MedicalClaim, Prescription, ProviderInvoice


class EncounterForm(forms.ModelForm):
    class Meta:
        model = Encounter
        fields = ["reason", "notes"]
        widgets = {
            "reason": forms.TextInput(attrs={"placeholder": "e.g. Annual physical / knee pain"}),
        }


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = ProviderInvoice
        fields = ["invoice_number", "reference", "memo"]
        widgets = {
            "invoice_number": forms.TextInput(attrs={"placeholder": "Statement #"}),
        }


class PrescriptionForm(forms.ModelForm):
    class Meta:
        model = Prescription
        fields = ["drug_name", "dosage", "reference", "memo"]
        widgets = {
            "drug_name": forms.TextInput(attrs={"placeholder": "e.g. Atorvastatin"}),
            "dosage": forms.TextInput(attrs={"placeholder": "e.g. 20 mg, 1 tablet daily"}),
            "reference": forms.TextInput(attrs={"placeholder": "Rx number"}),
        }


class ClaimForm(forms.ModelForm):
    class Meta:
        model = MedicalClaim
        fields = ["claim_number", "member_id", "group_number", "notes"]
        widgets = {
            "claim_number": forms.TextInput(attrs={"placeholder": "Claim # from the EOB"}),
            "member_id": forms.TextInput(attrs={"placeholder": "Member ID"}),
            "group_number": forms.TextInput(attrs={"placeholder": "Group #"}),
        }
