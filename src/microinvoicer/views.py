"""How about now."""
from datetime import date, timedelta, datetime
from django.db import transaction
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView, DeleteView, UpdateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.forms.models import model_to_dict
from django.template import Template, Context
from django_registration.backends.one_step.views import RegistrationView
from dateutil.rrule import rrule, MONTHLY
from django.apps import apps
from decimal import Decimal

from . import forms, models, pdf_rendering, micro_timesheet
from .temporary_locale import TemporaryLocale


def _apply_editable_invoice_fields(form, contract):
    """Fill in description and attached-cost fields shared by create and edit."""
    if form.cleaned_data.get("override_description"):
        form.instance.description = form.cleaned_data["override_description"]
    else:
        form.instance.description = contract.invoicing_description

    if form.cleaned_data.get("attached_cost") and form.cleaned_data.get("attached_description"):
        form.instance.attached_description = form.cleaned_data["attached_description"]
        form.instance.attached_cost = form.cleaned_data["attached_cost"]
    else:
        form.instance.attached_description = None
        form.instance.attached_cost = None


class IndexView(TemplateView):
    """Landing Page."""

    template_name = "index.html"


class MicroRegistrationView(RegistrationView):
    """User registration."""

    template_name = "registration_form.html"
    form_class = forms.MicroRegistrationForm
    # For now, we redirect straight to fiscal information view after signup.
    # When we'll change to two step registration, fiscal form will be shown at
    # the first login
    success_url = reverse_lazy("setup")


class MicroLoginView(LoginView):
    """Classic login."""

    template_name = "login.html"


class MicroHomeView(LoginRequiredMixin, TemplateView):
    """User Home."""

    template_name = "home.html"

    def get_context_data(self, **kwargs):
        """Attach all registry info, grouped by invoice status."""
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            user = self.request.user
            registries = list(
                user.registries.prefetch_related("seller", "contracts", "invoices").all()
            )
            for registry in registries:
                invoices = list(registry.invoices.all())
                drafts = [i for i in invoices if i.is_draft]
                published = [i for i in invoices if i.is_published]
                stornos = [i for i in invoices if i.is_storno]

                reversed_ids = {s.storno_of_id for s in stornos if s.storno_of_id}
                for invoice in published:
                    invoice.reversed_flag = invoice.id in reversed_ids

                official = sorted(published + stornos, key=lambda i: i.number or 0)
                registry.sorted_invoices = drafts + official
                registry.draft_count = len(drafts)
                registry.published_count = len(published)
                registry.storno_count = len(stornos)
                registry.net_total = sum((i.value for i in official), Decimal(0))

            context["registries"] = registries

        return context


class ReportView(LoginRequiredMixin, TemplateView):

    template_name = "report.html"

    def get_context_data(self, **kwargs):
        """Computes quarterly reports"""
        app_config = apps.get_app_config("microinvoicer")
        daily_rates = app_config.daily_rates
        daily_rates["ron"] = Decimal(1)
        average_rates = app_config.average_rates

        context = super().get_context_data(**kwargs)

        invoices = models.TimeInvoice.objects.filter(
            registry__user=self.request.user,
            status__in=[models.InvoiceStatus.PUBLISHED, models.InvoiceStatus.STORNO],
        ).order_by("-issue_date")

        # build up the monthly / quartery / yearly total
        totals = dict()
        if invoices:
            # fill in all spots between first and last invoice in reverse order
            since = invoices.last().issue_date.replace(day=1)
            until = invoices.first().issue_date.replace(day=1)
            all_months = list(rrule(freq=MONTHLY, dtstart=since, until=until, bymonthday=1))
            for every_month in reversed(all_months):
                year = every_month.year
                quarter = models.quarter_of(every_month)
                month = every_month.month

                if year not in totals:
                    totals[year] = dict(total=0)
                if quarter not in totals[year]:
                    totals[year][quarter] = dict(total=0)
                if month not in totals[year][quarter]:
                    totals[year][quarter][month] = dict(total=0, count=0, date=every_month)

        for invoice in invoices:
            year = invoice.issue_date.year
            quarter = models.quarter_of(invoice.issue_date)
            month = invoice.issue_date.month
            use_date = invoice.issue_date.replace(day=1)
            conversion_rate = average_rates.get(use_date, daily_rates[invoice.currency])

            value = invoice.value * conversion_rate

            total_year = totals.get(year, dict(total=0))
            total_year["total"] += value
            total_quarter = total_year.get(quarter, dict(total=0))
            total_quarter["total"] += value

            total_month = total_quarter.get(month, dict(total=0, count=0))
            total_month["total"] += value
            total_month["count"] += 1
            total_month["date"] = invoice.issue_date

            total_quarter[month] = total_month
            total_year[quarter] = total_quarter
            totals[year] = total_year

        context["totals"] = totals

        return context


class MicroFormMixin(LoginRequiredMixin):
    """Common requirements for model views"""

    template_name = "base_form.html"
    success_url = reverse_lazy("home")

    def get_context_data(self, **kwargs):
        """Add form title."""
        context = super().get_context_data(**kwargs)
        context["form_title"] = self.form_title
        return context


class ProfileUpdateView(MicroFormMixin, UpdateView):
    model = models.MicroUser
    template_name = "profile.html"
    form_title = "Your Profile"
    fields = ["first_name", "last_name"]

    def get_object(self):
        return self.request.user


class ProfileSetupView(ProfileUpdateView):
    model = models.MicroUser
    form_title = "Setup fiscal information"
    fields = ["email", "first_name", "last_name"]


class RegistryCreateView(MicroFormMixin, CreateView):
    model = models.MicroRegistry
    form_title = "Define new registry"
    form_class = forms.RegistryForm

    def form_valid(self, form):
        form.instance.user = self.request.user
        most_fields = [
            field.name for field in models.FiscalEntity._meta.get_fields() if field.name != "id"
        ]
        seller_data = {field: form.cleaned_data[field] for field in most_fields}
        seller = models.FiscalEntity(**seller_data)
        seller.save()
        form.instance.seller = seller
        return super().form_valid(form)


class RegistryUpdateView(MicroFormMixin, UpdateView):
    model = models.MicroRegistry
    form_title = "Update registry"
    form_class = forms.RegistryForm

    def form_valid(self, form):
        # update seller information too
        seller = form.instance.seller
        seller_fields = [
            field.name for field in models.FiscalEntity._meta.get_fields() if field.name != "id"
        ]
        for field in seller_fields:
            setattr(seller, field, form.cleaned_data[field])
        seller.save()
        return super().form_valid(form)

    def get_initial(self):
        initial = super().get_initial()
        if seller_instance := self.object.seller:
            seller_data = model_to_dict(
                seller_instance,
                fields=[
                    "name",
                    "owner_fullname",
                    "registration_id",
                    "fiscal_code",
                    "address",
                    "country",
                    "bank_account",
                    "bank_name",
                ],
            )
            initial.update(seller_data)
        return initial


class RegistryDeleteView(MicroFormMixin, DeleteView):
    model = models.MicroRegistry
    form_title = "Throwing away whole registry"
    template_name = "confirm_delete.html"


class ContractCreateView(MicroFormMixin, CreateView):
    model = models.ServiceContract
    form_title = "Register new contract"
    form_class = forms.ServiceContractForm

    def form_valid(self, form):
        """Create buyer instance before saving contract"""
        buyer_data = {
            field: form.cleaned_data[field]
            for field in form.cleaned_data
            if field in forms.FiscalEntityForm.declared_fields.keys()
        }
        buyer = models.FiscalEntity(**buyer_data)
        buyer.save()

        form.instance.buyer = buyer
        form.instance.registry = models.MicroRegistry.objects.get(pk=self.kwargs["registry_id"])

        return super().form_valid(form)


class ContractUpdateView(MicroFormMixin, UpdateView):
    model = models.ServiceContract
    form_title = "Modify contract"
    form_class = forms.ServiceContractForm

    def get_initial(self):
        initial = super().get_initial()
        if buyer_instance := self.object.buyer:
            buyer_data = model_to_dict(
                buyer_instance,
                fields=[
                    "name",
                    "owner_fullname",
                    "registration_id",
                    "fiscal_code",
                    "address",
                    "country",
                    "bank_account",
                    "bank_name",
                ],
            )
            initial.update(buyer_data)
        return initial

    def form_valid(self, form):
        """Update buyer instance before saving contract"""
        buyer_data = {
            field: form.cleaned_data[field]
            for field in form.cleaned_data
            if field in forms.FiscalEntityForm.declared_fields.keys()
        }
        form.instance.buyer.__dict__.update(**buyer_data)
        form.instance.buyer.save()

        return super().form_valid(form)


class ContractDeleteView(MicroFormMixin, DeleteView):
    model = models.ServiceContract
    form_title = "Throwing away contract"
    template_name = "confirm_delete.html"


class DraftOnlyMixin:
    """Restrict an action to invoices that are still drafts."""

    def dispatch(self, request, *args, **kwargs):
        invoice = self.get_object()
        if not invoice.is_draft:
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry_id,
                pk=invoice.pk,
            )
        return super().dispatch(request, *args, **kwargs)


class TimeInvoiceCreateView(MicroFormMixin, CreateView):
    model = models.TimeInvoice
    form_title = "Issue new time invoice"
    form_class = forms.TimeInvoiceForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["registry"] = self.kwargs["registry"]
        return kwargs

    def get_initial(self, **kwargs):
        """provide sensible defaults for a new invoice"""
        initial = super().get_initial()

        today = date.today()
        first_of_month = today.replace(day=1)
        last_month = first_of_month - timedelta(days=1)

        initial["issue_date"] = today
        registry = models.MicroRegistry.objects.get(pk=self.kwargs["registry_id"])
        initial["include_vat"] = registry.include_vat
        self.kwargs["registry"] = registry
        last_invoice = registry.invoices.filter(status=models.InvoiceStatus.PUBLISHED).last()
        if last_invoice:
            initial["contract"] = last_invoice.contract
            initial["quantity"] = last_invoice.quantity

            use_locale = "ro_RO" if last_invoice.contract.buyer.country == "RO" else "en_IE"
            with TemporaryLocale(use_locale):
                description_template = Template(last_invoice.contract.invoicing_description)
                local_context = Context(
                    dict(
                        this_month=date.strftime(today, "%B %Y").title(),
                        last_month=date.strftime(last_month, "%B %Y").title(),
                    )
                )
                initial["override_description"] = description_template.render(local_context)

        return initial

    def form_valid(self, form):
        """Save the new invoice as an editable draft (no number assigned yet)."""
        registry = self.kwargs["registry"]
        contract = form.instance.contract

        form.instance.registry = registry
        form.instance.seller = registry.seller
        form.instance.buyer = contract.buyer
        form.instance.series = registry.invoice_series
        form.instance.status = models.InvoiceStatus.DRAFT
        form.instance.currency = contract.invoicing_currency
        form.instance.unit = contract.unit
        form.instance.unit_rate = contract.unit_rate
        form.instance.include_vat = registry.include_vat

        _apply_editable_invoice_fields(form, contract)

        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "registry-invoice-detail",
            kwargs={"registry_id": self.object.registry_id, "pk": self.object.pk},
        )


class TimeInvoiceUpdateView(MicroFormMixin, DraftOnlyMixin, UpdateView):
    model = models.TimeInvoice
    form_title = "Edit draft invoice"
    form_class = forms.TimeInvoiceForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["registry"] = self.object.registry
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        initial["override_description"] = self.object.description
        return initial

    def form_valid(self, form):
        registry = self.object.registry
        contract = form.instance.contract

        form.instance.seller = registry.seller
        form.instance.buyer = contract.buyer
        form.instance.currency = contract.invoicing_currency
        form.instance.unit = contract.unit
        form.instance.unit_rate = contract.unit_rate
        form.instance.include_vat = registry.include_vat

        _apply_editable_invoice_fields(form, contract)

        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "registry-invoice-detail",
            kwargs={"registry_id": self.object.registry_id, "pk": self.object.pk},
        )


class TimeInvoiceDeleteView(MicroFormMixin, DraftOnlyMixin, DeleteView):
    model = models.TimeInvoice
    form_title = "Throwing away draft invoice"
    template_name = "confirm_delete.html"


class TimeInvoicePublishView(LoginRequiredMixin, View):
    """Promote a draft into a published, numbered invoice."""

    def post(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            models.TimeInvoice, pk=kwargs["pk"], registry__user=request.user
        )
        if invoice.is_draft:
            with transaction.atomic():
                registry = models.MicroRegistry.objects.select_for_update().get(
                    pk=invoice.registry_id
                )
                invoice.number = registry.next_invoice_no
                invoice.status = models.InvoiceStatus.PUBLISHED
                invoice.save(update_fields=["number", "status"])
                registry.next_invoice_no += 1
                registry.save(update_fields=["next_invoice_no"])
        return redirect(
            "registry-invoice-detail", registry_id=invoice.registry_id, pk=invoice.pk
        )


class TimeInvoiceStornoView(LoginRequiredMixin, View):
    """Issue a storno (red invoice) that reverses a published invoice."""

    def post(self, request, *args, **kwargs):
        original = get_object_or_404(
            models.TimeInvoice, pk=kwargs["pk"], registry__user=request.user
        )
        if not original.is_published or original.is_reversed:
            return redirect(
                "registry-invoice-detail",
                registry_id=original.registry_id,
                pk=original.pk,
            )
        with transaction.atomic():
            registry = models.MicroRegistry.objects.select_for_update().get(
                pk=original.registry_id
            )
            storno = models.TimeInvoice(
                registry=registry,
                seller=original.seller,
                buyer=original.buyer,
                contract=original.contract,
                storno_of=original,
                series=original.series,
                number=registry.next_invoice_no,
                status=models.InvoiceStatus.STORNO,
                description=(f"Storno of {original.series_number}: {original.description}")[
                    : models.LONG_TEXT
                ],
                currency=original.currency,
                conversion_rate=original.conversion_rate,
                unit=original.unit,
                unit_rate=original.unit_rate,
                attached_cost=(-original.attached_cost if original.attached_cost else None),
                attached_description=original.attached_description,
                issue_date=date.today(),
                quantity=-original.quantity,
                include_vat=original.include_vat,
            )
            storno.save()
            registry.next_invoice_no += 1
            registry.save(update_fields=["next_invoice_no"])
        return redirect(
            "registry-invoice-detail", registry_id=storno.registry_id, pk=storno.pk
        )


class TimeInvoiceDetailView(LoginRequiredMixin, DetailView):
    model = models.TimeInvoice
    template_name = "invoice_detail.html"


class TimeInvoicePrintView(LoginRequiredMixin, DetailView):
    """Download invoice as PDF file"""

    model = models.TimeInvoice
    response_class = FileResponse

    def render_to_response(self, context, **response_kwargs):
        """Returns content of generated pdf"""
        invoice = context["object"]
        if invoice.is_draft:
            return HttpResponseForbidden("Draft invoices cannot be printed.")
        content = pdf_rendering.render_invoice(invoice)
        response = FileResponse(
            content,
            filename=f"{invoice.series_number}.pdf",
            as_attachment=True,
            content_type="application/pdf",
        )
        return response


class TimeInvoiceFakeTimesheetView(LoginRequiredMixin, DetailView):
    """Generate fake timesheet as PDF file"""

    model = models.TimeInvoice
    response_class = FileResponse

    def render_to_response(self, context, **response_kwargs):
        """Returns content of generated pdf"""
        invoice = context["object"]
        if not invoice.is_published:
            return HttpResponseForbidden("Only published invoices have a timesheet.")
        if "last_month" in invoice.contract.invoicing_description:
            start_date = invoice.issue_date.replace(day=1) - timedelta(days=1)
            start_date = start_date.replace(day=1)
        else:
            start_date = invoice.issue_date.replace(day=1)
        timesheet = micro_timesheet.fake_timesheet(
            invoice.quantity, "Dashboard", "Web Application", start_date
        )
        content = pdf_rendering.render_timesheet(invoice, timesheet)
        response = FileResponse(
            content,
            filename=f"{invoice.series_number}-timesheet.pdf",
            as_attachment=True,
            content_type="application/pdf",
        )
        return response
