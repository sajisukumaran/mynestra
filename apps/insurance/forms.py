"""Insurance forms. A light ModelForm for the policy's free-text identity fields; policy type,
status, insurer, term dates, premium, currency, coverages, members and covered assets are hand-
rendered and parsed in the view (the app's htmx idiom, mirroring Automobile / Loans)."""

from django import forms

from apps.insurance.models import InsurancePolicy


class PolicyForm(forms.ModelForm):
    class Meta:
        model = InsurancePolicy
        fields = ["nickname", "plan_name", "policy_number", "notes"]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. Family auto policy"}),
            "plan_name": forms.TextInput(attrs={"placeholder": "e.g. Gold PPO / Full coverage"}),
        }
