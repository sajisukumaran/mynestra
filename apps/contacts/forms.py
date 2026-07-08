"""Person create/edit form. Text/select fields render as Django bound widgets wrapped in the kit's
`.control` styling; partial dates use the c-partial-date component; contact channels are edited as
an Alpine-managed array (parsed in the view). Addresses & important dates are edited on the detail
page, not here (lean, mockup-faithful form)."""

from django import forms
from django.core.exceptions import ValidationError

from apps.contacts.models import Person
from apps.core.partialdate import validate_partial_date

PARTIAL_DATES = [("dob", "date of birth"), ("dod", "date of death"), ("anniversary", "anniversary")]


class PersonForm(forms.ModelForm):
    languages = forms.CharField(required=False)  # comma-separated ↔ list on the model

    class Meta:
        model = Person
        fields = [
            "first_name", "middle_name", "last_name", "preferred_name", "gender", "pronouns",
            "photo",
            "dob_year", "dob_month", "dob_day",
            "marital_status", "anniversary_year", "anniversary_month", "anniversary_day",
            "blood_group", "occupation", "education", "dietary",
            "is_deceased", "dod_year", "dod_month", "dod_day",
            "notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        if self.instance and self.instance.pk:
            self.initial.setdefault("languages", ", ".join(self.instance.languages or []))

    def clean_languages(self):
        raw = self.cleaned_data.get("languages", "") or ""
        return [s.strip() for s in raw.split(",") if s.strip()]

    def clean(self):
        cleaned = super().clean()
        for prefix, _label in PARTIAL_DATES:
            try:
                validate_partial_date(
                    cleaned.get(f"{prefix}_year"),
                    cleaned.get(f"{prefix}_month"),
                    cleaned.get(f"{prefix}_day"),
                )
            except ValidationError as exc:
                self.add_error(f"{prefix}_day", exc)
        return cleaned

    def save(self, commit=True):
        self.instance.languages = self.cleaned_data.get("languages", [])
        return super().save(commit)
