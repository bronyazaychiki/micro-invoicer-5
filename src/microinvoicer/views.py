"""How about now."""
from datetime import date, timedelta, datetime
from django.http import FileResponse
from django.db import transaction
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


def _localized_description(contract, target_date):
    """Render a contract's monthly description template for `target_date`'s month."""
    first_of_month = target_date.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    use_locale = "ro_RO" if contract.buyer.country == "RO" else "en_IE"
    with TemporaryLocale(use_locale):
        description_template = Template(contract.invoicing_description)
        local_context = Context(
            dict(
                this_month=date.strftime(target_date, "%B %Y").title(),
                last_month=date.strftime(last_month, "%B %Y").title(),
            )
        )
        return description_template.render(local_context)


def _snapshot_contract_fields(invoice, registry, contract):
    """Copy seller/buyer/series and the pricing snapshot onto an invoice.

    Mirrors what the single-invoice flow has always stamped server-side. Does
    NOT assign `number` or `status` -- callers decide those, since drafts stay
    unnumbered until published.
    """
    invoice.registry = registry
    invoice.seller = registry.seller
    invoice.buyer = contract.buyer
    invoice.series = registry.invoice_series
    invoice.currency = contract.invoicing_currency
    invoice.unit = contract.unit
    invoice.unit_rate = contract.unit_rate
    invoice.include_vat = registry.include_vat


def _contract_last_invoice(contract):
    """Most recent invoice for a contract (by issue date), or None."""
    return (
        models.TimeInvoice.objects.filter(contract=contract)
        .order_by("-issue_date", "-id")
        .first()
    )


def _default_quantity(contract):
    last = _contract_last_invoice(contract)
    return last.quantity if last else 1


def _default_conversion_rate(contract):
    last = _contract_last_invoice(contract)
    if last and last.conversion_rate:
        return last.conversion_rate
    if contract.currency == contract.invoicing_currency:
        return Decimal(1)
    return None


def _parse_month(value):
    """Parse a 'YYYY-MM' string into the first day of that month; default to this month."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m").date().replace(day=1)
        except ValueError:
            pass
    return date.today().replace(day=1)


def _next_month(month):
    if month.month == 12:
        return month.replace(year=month.year + 1, month=1)
    return month.replace(month=month.month + 1)


def _issue_date_for_month(month):
    """A date guaranteed to fall in `month`: today if it's the current month,
    otherwise the last day of that month."""
    today = date.today()
    if month.year == today.year and month.month == today.month:
        return today
    return _next_month(month) - timedelta(days=1)


def _billing_overview(user, month):
    """Per-registry billing state for `month`, plus summary counts.

    A contract is 'billed' for `month` when it has any invoice whose issue_date
    falls in that month (drafts included, so re-running a batch won't duplicate).
    Counts: pending = contracts with no invoice this month; drafts/published =
    invoices of that status this month.
    """
    registries = user.registries.prefetch_related(
        "seller", "contracts", "contracts__buyer", "invoices"
    ).all()
    groups = []
    pending = drafts = published = 0
    for registry in registries:
        in_month = [
            inv
            for inv in registry.invoices.all()
            if inv.issue_date.year == month.year and inv.issue_date.month == month.month
        ]
        by_contract = {}
        for inv in in_month:
            by_contract.setdefault(inv.contract_id, []).append(inv)
            if inv.status == models.InvoiceStatus.DRAFT:
                drafts += 1
            elif inv.status == models.InvoiceStatus.PUBLISHED:
                published += 1
        rows = []
        for contract in registry.contracts.all():
            matching = by_contract.get(contract.id, [])
            if not matching:
                state = "pending"
                pending += 1
            elif any(i.status == models.InvoiceStatus.PUBLISHED for i in matching):
                state = "published"
            else:
                state = "draft"
            rows.append(
                dict(
                    contract=contract,
                    state=state,
                    duplicate=len(matching) > 1,
                    invoices=matching,
                    invoice=matching[0] if matching else None,
                )
            )
        groups.append(dict(registry=registry, rows=rows))
    summary = dict(pending=pending, drafts=drafts, published=published)
    return groups, summary


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
                "seller", "contracts", "invoices"
            ).all()

            month = date.today().replace(day=1)
            _, summary = _billing_overview(user, month)
            context["billing_month_label"] = month.strftime("%B %Y")
            context["billing_summary"] = summary

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
        initial["issue_date"] = today
        registry = models.MicroRegistry.objects.get(pk=self.kwargs["registry_id"])
        initial["include_vat"] = registry.include_vat
        self.kwargs["registry"] = registry
        last_invoice = registry.invoices.last()
        if last_invoice:
            initial["contract"] = last_invoice.contract
            initial["quantity"] = last_invoice.quantity
            initial["override_description"] = _localized_description(
                last_invoice.contract, today
            )

        return initial

    def form_valid(self, form):
        """Create the invoice as an unnumbered draft for later review/publish."""
        registry = self.kwargs["registry"]
        contract = form.instance.contract

        _snapshot_contract_fields(form.instance, registry, contract)
        form.instance.status = models.InvoiceStatus.DRAFT
        form.instance.number = None

        if form.cleaned_data["override_description"]:
            form.instance.description = form.cleaned_data["override_description"]
        else:
            form.instance.description = contract.invoicing_description

        if form.cleaned_data["attached_cost"] and form.cleaned_data["attached_description"]:
            form.instance.attached_description = form.cleaned_data["attached_description"]
            form.instance.attached_cost = form.cleaned_data["attached_cost"]

        return super().form_valid(form)

    def get_success_url(self):
        """Land on the new draft's detail page so it can be reviewed and published."""
        return reverse(
            "registry-invoice-detail",
            kwargs={"registry_id": self.object.registry_id, "pk": self.object.pk},
        )


class TimeInvoiceDeleteView(MicroFormMixin, DeleteView):
    model = models.TimeInvoice
    form_title = "Throwing away invoice"
    template_name = "confirm_delete.html"

    def delete(self, request, *args, **kwargs):
        invoice = self.get_object()
        registry = invoice.registry
        # Only roll the counter back when deleting the most recently issued,
        # numbered invoice. Drafts never consumed a number, so they must not
        # decrement it (that would corrupt the official sequence).
        if invoice.number is not None and invoice.number == registry.next_invoice_no - 1:
            registry.next_invoice_no -= 1
            registry.save()
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


class TimeInvoicePublishView(LoginRequiredMixin, View):
    """Allocate the official number and mark a draft invoice as published.

    The number is drawn from the registry's running counter inside a
    transaction that locks the registry row, so concurrent publishes can't
    collide or skip numbers (the old create flow did this unlocked, which could
    produce duplicates). Re-posting a published invoice is a no-op.
    """

    def post(self, request, *args, **kwargs):
        registry_id = self.kwargs["registry_id"]
        pk = self.kwargs["pk"]
        with transaction.atomic():
            registry = (
                models.MicroRegistry.objects.select_for_update()
                .filter(pk=registry_id, user=request.user)
                .first()
            )
            if registry is not None:
                invoice = get_object_or_404(models.TimeInvoice, pk=pk, registry=registry)
                if invoice.status == models.InvoiceStatus.DRAFT:
                    invoice.number = registry.next_invoice_no
                    invoice.status = models.InvoiceStatus.PUBLISHED
                    invoice.save()
                    registry.next_invoice_no += 1
                    registry.save()
        return redirect("registry-invoice-detail", registry_id=registry_id, pk=pk)


class BillingWorklistView(LoginRequiredMixin, TemplateView):
    """Monthly billing to-do: which of the current user's contracts still need
    an invoice for the selected period, which are drafted, which are published."""

    template_name = "worklist.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        month = _parse_month(self.request.GET.get("month"))
        groups, summary = _billing_overview(self.request.user, month)

        prev_month = (month - timedelta(days=1)).replace(day=1)
        context["groups"] = groups
        context["summary"] = summary
        context["month_label"] = month.strftime("%B %Y")
        context["month_param"] = month.strftime("%Y-%m")
        context["prev_month_param"] = prev_month.strftime("%Y-%m")
        context["next_month_param"] = _next_month(month).strftime("%Y-%m")
        context["created"] = self.request.GET.get("created")
        context["skipped"] = self.request.GET.get("skipped")
        return context


class BillingWorklistBatchCreateView(LoginRequiredMixin, View):
    """Create unnumbered DRAFT invoices for the selected contracts in one
    transaction. Skips any contract already billed in the target month so
    neither this batch nor a re-run produces duplicates, and never touches the
    official number sequence."""

    def post(self, request, *args, **kwargs):
        month = _parse_month(request.POST.get("month"))
        contract_ids = request.POST.getlist("contract_ids")

        created = 0
        skipped = 0
        if contract_ids:
            contracts = models.ServiceContract.objects.filter(
                pk__in=contract_ids, registry__user=request.user
            ).select_related("registry", "registry__seller", "buyer")
            issue_dt = _issue_date_for_month(month)
            with transaction.atomic():
                for contract in contracts:
                    already_billed = models.TimeInvoice.objects.filter(
                        contract=contract,
                        issue_date__year=month.year,
                        issue_date__month=month.month,
                    ).exists()
                    if already_billed:
                        skipped += 1
                        continue

                    invoice = models.TimeInvoice(contract=contract)
                    _snapshot_contract_fields(invoice, contract.registry, contract)
                    invoice.status = models.InvoiceStatus.DRAFT
                    invoice.number = None
                    invoice.issue_date = issue_dt
                    invoice.quantity = _default_quantity(contract)
                    invoice.conversion_rate = _default_conversion_rate(contract)
                    invoice.description = _localized_description(contract, issue_dt)
                    invoice.save()
                    created += 1

        url = reverse("billing-worklist")
        return redirect(
            f"{url}?month={month.strftime('%Y-%m')}&created={created}&skipped={skipped}"
        )
