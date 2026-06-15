from django.db import models
from django.db.models import Q
from django.core.mail import send_mail
from django.core.validators import MaxValueValidator, MinValueValidator
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.utils import timezone
from django_countries.fields import CountryField
from django_countries import countries as django_countries_list
from datetime import date

from .managers import MicroUserManager


LONG_TEXT = 255
SHORT_TEXT = 40
REALLY_SHORT = 16


def quarter_of(a_date: date):
    return f"Q{1 + (a_date.month - 1) // 3}"


class AvailableCurrencies(models.TextChoices):
    EUR = "eur", "Euros"
    USD = "usd", "US Dollars"
    RON = "ron", "Lei"


class InvoicingUnits(models.TextChoices):
    MONTHLY = "mo", "Month"
    DAILY = "d", "Day"
    HOURLY = "hr", "Hour"


class InvoiceStatus(models.IntegerChoices):
    DRAFT = 0, "Draft"
    PUBLISHED = 1, "Published"
    STORNO = 2, "Storno"


class FiscalEntity(models.Model):
    name = models.CharField(max_length=LONG_TEXT)
    owner_fullname = models.CharField(max_length=LONG_TEXT)
    registration_id = models.CharField(max_length=SHORT_TEXT)
    fiscal_code = models.CharField(max_length=SHORT_TEXT)
    address = models.TextField()
    country = CountryField(default="RO")
    bank_account = models.CharField(max_length=SHORT_TEXT)
    bank_name = models.CharField(max_length=LONG_TEXT)

    def __repr__(self) -> str:
        return f"{self.name}"

    def __str__(self):
        return repr(self)


class MicroUser(AbstractBaseUser, PermissionsMixin):
    """User account, which holds the service provider (seller) entity"""

    first_name = models.CharField(max_length=SHORT_TEXT)
    last_name = models.CharField(max_length=SHORT_TEXT)
    email = models.EmailField(unique=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = MicroUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    def get_full_name(self):
        full_name = f"{self.first_name} {self.last_name}"
        return full_name.strip()

    def get_short_name(self):
        return self.first_name

    def email_user(self, subject, message, from_email=None, **kwargs):
        send_mail(subject, message, from_email, [self.email], **kwargs)


class MicroRegistry(models.Model):
    user = models.ForeignKey(MicroUser, related_name="registries", on_delete=models.CASCADE)
    seller = models.ForeignKey(FiscalEntity, related_name="+", on_delete=models.CASCADE)

    display_name = models.CharField(max_length=SHORT_TEXT)
    invoice_series = models.CharField(max_length=REALLY_SHORT)
    next_invoice_no = models.IntegerField()
    include_vat = models.IntegerField(
        verbose_name="Apply VAT (%)",
        default=0,
        validators=(
            MinValueValidator(0),
            MaxValueValidator(100),
        ),
    )

    def __repr__(self) -> str:
        return f"{self.display_name}, series {self.invoice_series}, {self.contracts.count()} contracts and ..."

    def __str__(self):
        return repr(self)


class Client(models.Model):
    """Master data for a buyer/client, scoped to a specific registry."""

    registry = models.ForeignKey(
        MicroRegistry, related_name="clients", on_delete=models.CASCADE
    )

    name = models.CharField(max_length=LONG_TEXT)
    owner_fullname = models.CharField(max_length=LONG_TEXT)
    registration_id = models.CharField(max_length=SHORT_TEXT)
    fiscal_code = models.CharField(max_length=SHORT_TEXT)
    address = models.TextField()
    country = CountryField(default="RO")
    bank_account = models.CharField(max_length=SHORT_TEXT)
    bank_name = models.CharField(max_length=LONG_TEXT)

    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["registry", "fiscal_code"],
                name="unique_client_fiscal_per_registry",
            ),
        ]

    def __repr__(self) -> str:
        return f"{self.name}"

    def __str__(self):
        return repr(self)

    @property
    def invoices(self):
        return TimeInvoice.objects.filter(contract__client=self)


class ServiceContract(models.Model):
    client = models.ForeignKey(Client, related_name="contracts", on_delete=models.RESTRICT)
    registry = models.ForeignKey(MicroRegistry, related_name="contracts", on_delete=models.CASCADE)

    registration_no = models.CharField("Contract number", max_length=SHORT_TEXT)
    registration_date = models.DateField("Contract date")
    currency = models.CharField(max_length=3, choices=AvailableCurrencies.choices)
    unit = models.CharField(max_length=2, choices=InvoicingUnits.choices)
    unit_rate = models.DecimalField(max_digits=16, decimal_places=2)
    invoicing_currency = models.CharField(max_length=3, choices=AvailableCurrencies.choices)
    invoicing_description = models.CharField(
        "Service description template", max_length=LONG_TEXT, blank=True
    )

    def __repr__(self) -> str:
        return f"{self.client!r}, {self.unit_rate} {self.currency}/{self.unit}"

    def __str__(self):
        return repr(self)


class TimeInvoice(models.Model):
    registry = models.ForeignKey(MicroRegistry, related_name="invoices", on_delete=models.CASCADE)
    seller = models.ForeignKey(
        FiscalEntity, related_name="+", on_delete=models.SET_NULL, null=True, blank=True
    )
    buyer = models.ForeignKey(
        FiscalEntity, related_name="+", on_delete=models.SET_NULL, null=True, blank=True
    )
    contract = models.ForeignKey(ServiceContract, related_name="+", on_delete=models.RESTRICT)

    series = models.CharField(max_length=REALLY_SHORT)
    number = models.IntegerField()
    status = models.IntegerField(choices=InvoiceStatus.choices)
    description = models.CharField(max_length=LONG_TEXT, blank=True)
    currency = models.CharField(max_length=3, choices=AvailableCurrencies.choices)
    conversion_rate = models.DecimalField(
        max_digits=16, decimal_places=4, null=True
    )  # contract currency to invoice currency
    unit = models.CharField(max_length=2, choices=InvoicingUnits.choices)
    unit_rate = models.DecimalField(max_digits=16, decimal_places=2)

    attached_cost = models.DecimalField(max_digits=16, decimal_places=2, blank=True, null=True)
    attached_description = models.CharField(max_length=LONG_TEXT, blank=True, null=True)

    issue_date = models.DateField()
    quantity = models.IntegerField()
    include_vat = models.IntegerField(default=0)

    # Buyer snapshot at time of invoice creation
    buyer_snapshot_name = models.CharField(max_length=LONG_TEXT, blank=True)
    buyer_snapshot_owner_fullname = models.CharField(max_length=LONG_TEXT, blank=True)
    buyer_snapshot_registration_id = models.CharField(max_length=SHORT_TEXT, blank=True)
    buyer_snapshot_fiscal_code = models.CharField(max_length=SHORT_TEXT, blank=True)
    buyer_snapshot_address = models.TextField(blank=True)
    buyer_snapshot_country = models.CharField(max_length=SHORT_TEXT, blank=True)
    buyer_snapshot_bank_account = models.CharField(max_length=SHORT_TEXT, blank=True)
    buyer_snapshot_bank_name = models.CharField(max_length=LONG_TEXT, blank=True)

    # Seller snapshot at time of invoice creation
    seller_snapshot_name = models.CharField(max_length=LONG_TEXT, blank=True)
    seller_snapshot_owner_fullname = models.CharField(max_length=LONG_TEXT, blank=True)
    seller_snapshot_registration_id = models.CharField(max_length=SHORT_TEXT, blank=True)
    seller_snapshot_fiscal_code = models.CharField(max_length=SHORT_TEXT, blank=True)
    seller_snapshot_address = models.TextField(blank=True)
    seller_snapshot_country = models.CharField(max_length=SHORT_TEXT, blank=True)
    seller_snapshot_bank_account = models.CharField(max_length=SHORT_TEXT, blank=True)
    seller_snapshot_bank_name = models.CharField(max_length=LONG_TEXT, blank=True)

    @property
    def series_number(self):
        return f"{self.series}-{self.number:04}"

    @property
    def value(self):
        return self.time_value() + self.vat_value() + (self.attached_cost or 0)

    def time_value(self):
        conversion = self.conversion_rate or 1
        return self.unit_rate * self.quantity * conversion

    def vat_value(self):
        return (self.time_value() * self.include_vat) / 100

    @property
    def contract_currency(self):
        return self.contract.currency

    @property
    def buyer_snapshot_country_display(self):
        country_dict = dict(django_countries_list)
        return country_dict.get(self.buyer_snapshot_country, self.buyer_snapshot_country)

    @property
    def seller_snapshot_country_display(self):
        country_dict = dict(django_countries_list)
        return country_dict.get(self.seller_snapshot_country, self.seller_snapshot_country)

    def populate_buyer_snapshot(self, client):
        """Copy current client data into buyer snapshot fields."""
        self.buyer_snapshot_name = client.name
        self.buyer_snapshot_owner_fullname = client.owner_fullname
        self.buyer_snapshot_registration_id = client.registration_id
        self.buyer_snapshot_fiscal_code = client.fiscal_code
        self.buyer_snapshot_address = client.address
        self.buyer_snapshot_country = str(client.country)
        self.buyer_snapshot_bank_account = client.bank_account
        self.buyer_snapshot_bank_name = client.bank_name

    def populate_seller_snapshot(self, seller_entity):
        """Copy current seller FiscalEntity data into seller snapshot fields."""
        self.seller_snapshot_name = seller_entity.name
        self.seller_snapshot_owner_fullname = seller_entity.owner_fullname
        self.seller_snapshot_registration_id = seller_entity.registration_id
        self.seller_snapshot_fiscal_code = seller_entity.fiscal_code
        self.seller_snapshot_address = seller_entity.address
        self.seller_snapshot_country = str(seller_entity.country)
        self.seller_snapshot_bank_account = seller_entity.bank_account
        self.seller_snapshot_bank_name = seller_entity.bank_name

    def __repr__(self) -> str:
        return f"{self.series_number} for {self.buyer_snapshot_name}"

    def __str__(self):
        return repr(self)


# ---------------------------------------------------------------------------
# Utility: duplicate detection
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = [
    " srl", " s.r.l.", " s.r.l", " sa", " s.a.",
    " ltd", " ltd.", " llc", " llc.", " gmbh", " ag", " bv", " nv",
    " inc", " inc.", " limited", " co", " co.",
]


def _normalize_company_name(name):
    """Strip common corporate suffixes and normalize for comparison."""
    if not name:
        return ""
    cleaned = name.strip().lower()
    for suffix in _COMPANY_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    return cleaned


def find_duplicate_clients(registry, name, fiscal_code, exclude_pk=None):
    """
    Find potential duplicate clients within a registry.
    Returns a list of matching Client objects.

    Detection criteria (OR logic):
    - Exact match on fiscal_code (case-insensitive, trimmed)
    - Normalized name match (lowercased, stripped of common suffixes)
    """
    qs = Client.objects.filter(registry=registry)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)

    matches = {}

    # Exact fiscal code match
    normalized_fiscal = fiscal_code.strip().upper() if fiscal_code else ""
    if normalized_fiscal:
        for c in qs.filter(fiscal_code__iexact=normalized_fiscal):
            matches[c.pk] = c

    # Normalized name match (done in Python since normalization strips suffixes)
    normalized_name = _normalize_company_name(name)
    if normalized_name:
        for c in qs.exclude(pk__in=matches):
            if _normalize_company_name(c.name) == normalized_name:
                matches[c.pk] = c

    return list(matches.values())
