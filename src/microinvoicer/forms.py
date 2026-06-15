from decimal import Decimal

from django import forms
from django_registration.forms import RegistrationForm
from django_countries.fields import CountryField
from material import Layout, Row
from . import models


class MicroRegistrationForm(RegistrationForm):
    class Meta(RegistrationForm.Meta):
        model = models.MicroUser
        fields = ["email", "first_name", "last_name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True

    layout = Layout(Row("email"), Row("first_name", "last_name"), Row("password1", "password2"))


class EditablesMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields:
            if field not in self.editables:
                self.fields[field].disabled = True


class FiscalEntityForm(forms.ModelForm):
    name = forms.CharField(max_length=models.LONG_TEXT, label="Company name")
    owner_fullname = forms.CharField(max_length=models.LONG_TEXT)
    registration_id = forms.CharField(max_length=models.SHORT_TEXT)
    fiscal_code = forms.CharField(max_length=models.SHORT_TEXT)
    address = forms.CharField(widget=forms.Textarea)
    country = CountryField().formfield()
    bank_account = forms.CharField(max_length=models.SHORT_TEXT)
    bank_name = forms.CharField(max_length=models.LONG_TEXT)


class RegistryForm(FiscalEntityForm):
    class Meta:
        model = models.MicroRegistry
        fields = ["display_name", "invoice_series", "next_invoice_no", "include_vat"]


class ServiceContractForm(FiscalEntityForm):
    class Meta:
        model = models.ServiceContract
        fields = [
            "registration_no",
            "registration_date",
            "unit_rate",
            "currency",
            "unit",
            "invoicing_currency",
            "invoicing_description",
            "default_project",
            "task_presets",
        ]

    def __init__(self, *args, **kwargs):
        """reorder fields to get buyer details on top"""
        super().__init__(*args, **kwargs)
        buyer_fields = {
            key: value for key, value in self.fields.items() if key not in self.Meta.fields
        }
        self_fields = {key: value for key, value in self.fields.items() if key in self.Meta.fields}
        self.fields = dict(**buyer_fields, **self_fields)


class TimeInvoiceForm(forms.ModelForm):
    conversion_rate = forms.DecimalField(
        required=False, help_text="to local currency (if applicable)"
    )
    override_description = forms.CharField(required=False, help_text="(optional)")

    class Meta:
        model = models.TimeInvoice
        fields = [
            "contract",
            "issue_date",
            "quantity",
            "conversion_rate",
            "attached_description",
            "attached_cost",
        ]

    def __init__(self, *args, **kwargs):
        registry = kwargs.pop("registry")
        super().__init__(*args, **kwargs)
        self.fields["contract"].queryset = models.ServiceContract.objects.filter(registry=registry)


class TimesheetEntryForm(forms.ModelForm):
    class Meta:
        model = models.TimesheetEntry
        fields = ["work_date", "project", "activity", "hours"]
        widgets = {
            "work_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "activity": forms.TextInput(attrs={"list": "task-presets", "autocomplete": "off"}),
            "hours": forms.NumberInput(attrs={"step": "0.25", "min": "0"}),
        }


class BaseTimesheetFormSet(forms.BaseInlineFormSet):
    """Enforces that the timesheet hours add up to the invoiced quantity."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        total = Decimal("0")
        rows = 0
        for form in self.forms:
            cleaned = getattr(form, "cleaned_data", None)
            if not cleaned or cleaned.get("DELETE"):
                continue
            hours = cleaned.get("hours")
            if hours is None:
                continue
            total += hours
            rows += 1

        if rows == 0:
            raise forms.ValidationError("Add at least one timesheet entry.")

        required = Decimal(self.instance.quantity)
        if total != required:
            raise forms.ValidationError(
                f"Timesheet hours total {total:g} but this invoice is for {required:g}. "
                f"Adjust the entries so they add up to exactly {required:g}."
            )


TimesheetEntryFormSet = forms.inlineformset_factory(
    models.TimeInvoice,
    models.TimesheetEntry,
    form=TimesheetEntryForm,
    formset=BaseTimesheetFormSet,
    extra=3,
    can_delete=True,
)
