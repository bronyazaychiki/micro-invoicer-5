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


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

_CLIENT_FIELDS = [
    "name",
    "owner_fullname",
    "registration_id",
    "fiscal_code",
    "address",
    "country",
    "bank_account",
    "bank_name",
]


class ClientForm(forms.ModelForm):
    """Standalone form for creating / editing a Client."""

    class Meta:
        model = models.Client
        fields = _CLIENT_FIELDS + ["notes"]

    def __init__(self, *args, registry=None, **kwargs):
        self.registry = registry
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        if self.registry:
            # Hard block: the (registry, fiscal_code) unique constraint would otherwise
            # surface as an IntegrityError 500. Turn it into a friendly field error.
            fiscal = (cleaned.get("fiscal_code") or "").strip()
            if fiscal:
                clash = (
                    models.Client.objects.filter(
                        registry=self.registry, fiscal_code__iexact=fiscal
                    )
                    .exclude(pk=self.instance.pk)
                    .first()
                )
                if clash:
                    self.add_error(
                        "fiscal_code",
                        "A client with this fiscal code already exists in this registry.",
                    )
            # Soft warning: similar name (possibly a different entity) — user may confirm.
            duplicates = models.find_duplicate_clients(
                registry=self.registry,
                name=cleaned.get("name", ""),
                fiscal_code=cleaned.get("fiscal_code", ""),
                exclude_pk=self.instance.pk,
            )
            if duplicates:
                cleaned["duplicate_matches"] = duplicates
        return cleaned


class ContractClientForm(forms.ModelForm):
    """Contract form with client selector + optional inline new client fields."""

    existing_client = forms.ModelChoiceField(
        queryset=models.Client.objects.none(),
        required=False,
        label="Select existing client",
        empty_label="-- or create new below --",
    )

    # Inline new-client fields (all optional; required only if existing_client empty)
    new_name = forms.CharField(max_length=models.LONG_TEXT, required=False, label="Company name")
    new_owner_fullname = forms.CharField(max_length=models.LONG_TEXT, required=False)
    new_registration_id = forms.CharField(max_length=models.SHORT_TEXT, required=False)
    new_fiscal_code = forms.CharField(max_length=models.SHORT_TEXT, required=False)
    new_address = forms.CharField(required=False, widget=forms.Textarea)
    new_country = CountryField().formfield(required=False)
    new_bank_account = forms.CharField(max_length=models.SHORT_TEXT, required=False)
    new_bank_name = forms.CharField(max_length=models.LONG_TEXT, required=False)

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
        ]

    def __init__(self, *args, registry=None, **kwargs):
        self.registry = registry
        super().__init__(*args, **kwargs)
        if registry:
            self.fields["existing_client"].queryset = models.Client.objects.filter(
                registry=registry
            )
        # Reorder: client selection first, then new-client fields, then contract fields
        field_order = (
            ["existing_client"]
            + [f for f in self.fields if f.startswith("new_")]
            + self.Meta.fields
        )
        self.order_fields(field_order)

    def clean(self):
        cleaned = super().clean()
        existing = cleaned.get("existing_client")
        new_name = (cleaned.get("new_name") or "").strip()

        if not existing and not new_name:
            raise forms.ValidationError(
                "Either select an existing client or enter a new client name."
            )

        if not existing and new_name:
            required_new = ["new_registration_id", "new_fiscal_code", "new_address"]
            for field_name in required_new:
                if not (cleaned.get(field_name) or "").strip():
                    self.add_error(
                        field_name, "This field is required when creating a new client."
                    )

            # Check for duplicates among new client data
            if self.registry:
                duplicates = models.find_duplicate_clients(
                    registry=self.registry,
                    name=new_name,
                    fiscal_code=(cleaned.get("new_fiscal_code") or "").strip(),
                )
                if duplicates:
                    cleaned["duplicate_matches"] = duplicates

        return cleaned

    def get_or_create_client(self):
        """Called from the view after form validation to resolve the client."""
        existing = self.cleaned_data.get("existing_client")
        if existing:
            return existing
        # If a client with this exact fiscal code already exists in the registry,
        # reuse it rather than violating the unique constraint — this is the same
        # entity, and reusing is exactly the de-duplication the user asked for.
        fiscal = (self.cleaned_data.get("new_fiscal_code") or "").strip()
        if fiscal:
            match = models.Client.objects.filter(
                registry=self.registry, fiscal_code__iexact=fiscal
            ).first()
            if match:
                return match
        return models.Client.objects.create(
            registry=self.registry,
            name=self.cleaned_data["new_name"],
            owner_fullname=self.cleaned_data.get("new_owner_fullname", ""),
            registration_id=self.cleaned_data["new_registration_id"],
            fiscal_code=self.cleaned_data["new_fiscal_code"],
            address=self.cleaned_data["new_address"],
            country=self.cleaned_data.get("new_country") or "RO",
            bank_account=self.cleaned_data.get("new_bank_account", ""),
            bank_name=self.cleaned_data.get("new_bank_name", ""),
        )


# ---------------------------------------------------------------------------
# Invoice form (unchanged logic)
# ---------------------------------------------------------------------------


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
