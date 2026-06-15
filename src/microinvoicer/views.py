"""How about now."""
from datetime import date, timedelta, datetime
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import TemplateView, ListView
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
        """Attach all registry info."""
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            user = self.request.user
            context["registries"] = user.registries.prefetch_related(
                "seller", "contracts", "invoices", "clients"
            ).all()

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

        invoices = models.TimeInvoice.objects.filter(registry__user=self.request.user).order_by(
            "-issue_date"
        )

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


# ---------------------------------------------------------------------------
# Client CRUD views
# ---------------------------------------------------------------------------


class _RegistryScopedMixin:
    """Ensures the view is scoped to a registry the user owns."""

    def get_registry(self):
        return get_object_or_404(
            models.MicroRegistry,
            pk=self.kwargs["registry_id"],
            user=self.request.user,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["registry"] = self.get_registry()
        return context


class ClientListView(_RegistryScopedMixin, LoginRequiredMixin, ListView):
    model = models.Client
    template_name = "client_list.html"
    context_object_name = "clients"

    def get_queryset(self):
        self.registry = self.get_registry()
        return (
            models.Client.objects.filter(registry=self.registry)
            .prefetch_related("contracts")
            .order_by("name")
        )


class ClientCreateView(_RegistryScopedMixin, LoginRequiredMixin, CreateView):
    model = models.Client
    form_class = forms.ClientForm
    form_title = "Add new client"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["registry"] = self.get_registry()
        return kwargs

    def form_valid(self, form):
        registry = self.get_registry()
        # Check for duplicates — if found and not confirmed, re-render with warning
        duplicates = form.cleaned_data.get("duplicate_matches")
        if duplicates and not self.request.POST.get("confirm_create"):
            return self.render_to_response(
                self.get_context_data(form=form, duplicate_matches=duplicates)
            )
        form.instance.registry = registry
        self.success_url = reverse_lazy(
            "registry-client-list", kwargs={"registry_id": registry.pk}
        )
        return super().form_valid(form)


class ClientDetailView(_RegistryScopedMixin, LoginRequiredMixin, DetailView):
    model = models.Client
    template_name = "client_detail.html"
    context_object_name = "client"

    def get_queryset(self):
        registry = self.get_registry()
        return models.Client.objects.filter(registry=registry)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.object
        context["contracts"] = client.contracts.select_related("registry").all()
        context["invoices"] = models.TimeInvoice.objects.filter(
            contract__client=client
        ).select_related("contract").order_by("-issue_date")
        return context


class ClientUpdateView(_RegistryScopedMixin, LoginRequiredMixin, UpdateView):
    model = models.Client
    form_class = forms.ClientForm
    form_title = "Edit client"

    def get_queryset(self):
        registry = self.get_registry()
        return models.Client.objects.filter(registry=registry)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["registry"] = self.get_registry()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.object
        context["contract_count"] = client.contracts.count()
        context["invoice_count"] = models.TimeInvoice.objects.filter(
            contract__client=client
        ).count()
        return context

    def form_valid(self, form):
        duplicates = form.cleaned_data.get("duplicate_matches")
        if duplicates and not self.request.POST.get("confirm_create"):
            return self.render_to_response(
                self.get_context_data(form=form, duplicate_matches=duplicates)
            )
        self.success_url = reverse_lazy(
            "registry-client-detail",
            kwargs={"registry_id": self.get_registry().pk, "pk": self.object.pk},
        )
        return super().form_valid(form)


class ClientDeleteView(_RegistryScopedMixin, LoginRequiredMixin, DeleteView):
    model = models.Client
    template_name = "confirm_delete.html"
    form_title = "Delete client"

    def get_queryset(self):
        registry = self.get_registry()
        return models.Client.objects.filter(registry=registry)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.object
        contract_count = client.contracts.count()
        context["contract_count"] = contract_count
        if contract_count > 0:
            context["delete_blocked"] = True
            context["block_reason"] = (
                f"This client has {contract_count} contract(s) and cannot be deleted. "
                "Remove or reassign the contracts first."
            )
        return context

    def get_success_url(self):
        return reverse_lazy(
            "registry-client-list",
            kwargs={"registry_id": self.kwargs["registry_id"]},
        )

    def form_valid(self, form):
        # On Django 4.2, DeleteView handles POST via form_valid (not delete()), so the
        # guard must live here to actually run. A client still referenced by contracts
        # is RESTRICT-protected at the DB; block it cleanly instead of letting it 500.
        if self.object.contracts.exists():
            return self.render_to_response(self.get_context_data())
        return super().form_valid(form)


class ClientDuplicateCheckView(_RegistryScopedMixin, LoginRequiredMixin, View):
    """AJAX endpoint: returns JSON list of potential duplicate clients."""

    def get(self, request, *args, **kwargs):
        registry = self.get_registry()
        name = request.GET.get("name", "").strip()
        fiscal_code = request.GET.get("fiscal_code", "").strip()
        exclude_pk = request.GET.get("exclude_pk")

        matches = models.find_duplicate_clients(
            registry=registry,
            name=name,
            fiscal_code=fiscal_code,
            exclude_pk=exclude_pk,
        )
        data = [
            {"id": c.pk, "name": c.name, "fiscal_code": c.fiscal_code}
            for c in matches
        ]
        return JsonResponse(data, safe=False)


# ---------------------------------------------------------------------------
# Contract views (rewritten for Client)
# ---------------------------------------------------------------------------


class ContractCreateView(MicroFormMixin, CreateView):
    model = models.ServiceContract
    form_title = "Register new contract"
    form_class = forms.ContractClientForm

    def _get_registry(self):
        return models.MicroRegistry.objects.get(pk=self.kwargs["registry_id"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["registry"] = self._get_registry()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["registry"] = self._get_registry()
        return context

    def form_valid(self, form):
        """Resolve client from selector or inline creation."""
        # Check for duplicates in new client — if found and not confirmed, re-render
        duplicates = form.cleaned_data.get("duplicate_matches")
        if duplicates and not self.request.POST.get("confirm_create"):
            return self.render_to_response(
                self.get_context_data(form=form, duplicate_matches=duplicates)
            )

        registry = models.MicroRegistry.objects.get(pk=self.kwargs["registry_id"])
        client = form.get_or_create_client()
        form.instance.client = client
        form.instance.registry = registry
        return super().form_valid(form)


class ContractUpdateView(MicroFormMixin, UpdateView):
    model = models.ServiceContract
    form_title = "Modify contract"
    form_class = forms.ContractClientForm

    def _get_registry(self):
        return models.MicroRegistry.objects.get(pk=self.kwargs["registry_id"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["registry"] = self._get_registry()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["registry"] = self._get_registry()
        return context

    def get_initial(self):
        initial = super().get_initial()
        initial["existing_client"] = self.object.client
        return initial

    def form_valid(self, form):
        """Resolve client from selector or inline creation."""
        duplicates = form.cleaned_data.get("duplicate_matches")
        if duplicates and not self.request.POST.get("confirm_create"):
            return self.render_to_response(
                self.get_context_data(form=form, duplicate_matches=duplicates)
            )
        new_client = form.get_or_create_client()
        form.instance.client = new_client
        return super().form_valid(form)


class ContractDeleteView(MicroFormMixin, DeleteView):
    model = models.ServiceContract
    form_title = "Throwing away contract"
    template_name = "confirm_delete.html"

    def _invoice_count(self):
        return models.TimeInvoice.objects.filter(contract=self.object).count()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invoice_count = self._invoice_count()
        context["invoice_count"] = invoice_count
        if invoice_count > 0:
            context["delete_blocked"] = True
            context["block_reason"] = (
                f"This contract has {invoice_count} invoice(s) and cannot be deleted. "
                "Historical invoices must keep their originating contract."
            )
        return context

    def form_valid(self, form):
        # Deleting a contract referenced by invoices would raise a DB RestrictedError.
        # Block it here (on the actual POST path) and re-show the reason instead of 500ing.
        if self._invoice_count() > 0:
            return self.render_to_response(self.get_context_data())
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Invoice views
# ---------------------------------------------------------------------------


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
        last_invoice = registry.invoices.last()
        if last_invoice:
            initial["contract"] = last_invoice.contract
            initial["quantity"] = last_invoice.quantity

            use_locale = "ro_RO" if last_invoice.contract.client.country == "RO" else "en_IE"
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
        """Fill in the missing fields and populate snapshots."""
        registry = self.kwargs["registry"]
        contract = form.instance.contract
        client = contract.client

        form.instance.registry = registry
        # Legacy FKs — set to None (snapshots are the source of truth now)
        form.instance.seller = None
        form.instance.buyer = None
        form.instance.series = registry.invoice_series
        form.instance.number = registry.next_invoice_no
        form.instance.status = models.InvoiceStatus.PUBLISHED
        form.instance.currency = contract.invoicing_currency
        form.instance.unit = contract.unit
        form.instance.unit_rate = contract.unit_rate
        form.instance.include_vat = registry.include_vat

        # Populate snapshots
        form.instance.populate_buyer_snapshot(client)
        form.instance.populate_seller_snapshot(registry.seller)

        if form.cleaned_data["override_description"]:
            form.instance.description = form.cleaned_data["override_description"]
        else:
            form.instance.description = contract.invoicing_description

        if form.cleaned_data["attached_cost"] and form.cleaned_data["attached_description"]:
            form.instance.attached_description = form.cleaned_data["attached_description"]
            form.instance.attached_cost = form.cleaned_data["attached_cost"]

        response = super().form_valid(form)
        registry.next_invoice_no += 1
        registry.save()
        return response


class TimeInvoiceDeleteView(MicroFormMixin, DeleteView):
    model = models.TimeInvoice
    form_title = "Throwing away invoice"
    template_name = "confirm_delete.html"

    def delete(self, request, *args, **kwargs):
        invoice = self.get_object()
        invoice.registry.next_invoice_no -= 1
        invoice.registry.save()
        return super().delete(request, *args, **kwargs)


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
