"""How about now."""
import calendar
from datetime import date, timedelta, datetime
from django.http import FileResponse
from django.urls import reverse_lazy, reverse
from django.views.generic import TemplateView
from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView, DeleteView, UpdateView
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.db import transaction
from django.forms.models import model_to_dict
from django.template import Template, Context
from django_registration.backends.one_step.views import RegistrationView
from dateutil.rrule import rrule, MONTHLY
from django.apps import apps
from decimal import Decimal

from . import forms, models, pdf_rendering, micro_timesheet
from .temporary_locale import TemporaryLocale


def render_contract_description(contract, target_date):
    """Render a contract's invoicing_description template for a given date."""
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

            # Monthly billing stats for current month
            today = date.today()
            invoices_this_month = models.TimeInvoice.objects.filter(
                registry__user=user,
                issue_date__year=today.year,
                issue_date__month=today.month,
            )
            pending_count = (
                models.ServiceContract.objects.filter(
                    registry__user=user,
                    is_active=True,
                    unit=models.InvoicingUnits.MONTHLY,
                )
                .exclude(
                    pk__in=models.TimeInvoice.objects.filter(
                        issue_date__year=today.year,
                        issue_date__month=today.month,
                    ).values_list("contract_id", flat=True)
                )
                .count()
            )
            context["monthly_stats"] = {
                "draft_count": invoices_this_month.filter(
                    status=models.InvoiceStatus.DRAFT
                ).count(),
                "published_count": invoices_this_month.filter(
                    status=models.InvoiceStatus.PUBLISHED
                ).count(),
                "pending_count": pending_count,
            }

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
            initial["override_description"] = render_contract_description(
                last_invoice.contract, today
            )

        return initial

    def form_valid(self, form):
        """Fill in the missing fields"""
        registry = self.kwargs["registry"]
        contract = form.instance.contract

        form.instance.registry = registry
        form.instance.seller = registry.seller
        form.instance.buyer = contract.buyer
        form.instance.series = registry.invoice_series
        form.instance.number = registry.next_invoice_no
        form.instance.status = models.InvoiceStatus.DRAFT
        form.instance.currency = contract.invoicing_currency
        form.instance.unit = contract.unit
        form.instance.unit_rate = contract.unit_rate
        form.instance.include_vat = registry.include_vat

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

    def dispatch(self, request, *args, **kwargs):
        invoice = self.get_object()
        if invoice.status != models.InvoiceStatus.DRAFT:
            messages.error(request, "Only draft invoices can be deleted.")
            return redirect("home")
        return super().dispatch(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        invoice = self.get_object()
        if invoice.number == invoice.registry.next_invoice_no - 1:
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


class InvoicePublishView(LoginRequiredMixin, View):
    """Publish a draft invoice."""

    def post(self, request, registry_id, pk):
        invoice = get_object_or_404(models.TimeInvoice, pk=pk, registry_id=registry_id)
        if invoice.status != models.InvoiceStatus.DRAFT:
            messages.error(request, "Only draft invoices can be published.")
            return redirect("home")
        invoice.status = models.InvoiceStatus.PUBLISHED
        invoice.save()
        messages.success(request, f"Invoice {invoice.series_number} published.")
        return redirect(
            reverse("registry-invoice-detail", kwargs={"registry_id": registry_id, "pk": pk})
        )


def _create_draft_invoice(contract, issue_date, registry):
    """Create a single draft invoice for a contract. Must be called inside transaction.atomic()."""
    number = registry.allocate_invoice_number()
    description = render_contract_description(contract, issue_date)
    return models.TimeInvoice.objects.create(
        registry=registry,
        seller=registry.seller,
        buyer=contract.buyer,
        contract=contract,
        series=registry.invoice_series,
        number=number,
        status=models.InvoiceStatus.DRAFT,
        description=description,
        currency=contract.invoicing_currency,
        unit=contract.unit,
        unit_rate=contract.unit_rate,
        issue_date=issue_date,
        quantity=1,
        include_vat=registry.include_vat,
    )


class SingleDraftView(LoginRequiredMixin, View):
    """Quick-create a single draft invoice from the billing todo."""

    def post(self, request, registry_id, contract_pk):
        registry = get_object_or_404(
            models.MicroRegistry, pk=registry_id, user=request.user
        )
        contract = get_object_or_404(
            models.ServiceContract, pk=contract_pk, registry=registry, is_active=True
        )

        # Parse target month from POST data or default to current month
        try:
            target_year = int(request.POST.get("target_year", date.today().year))
            target_month = int(request.POST.get("target_month", date.today().month))
            issue_date = date(target_year, target_month, 1)
        except (ValueError, TypeError):
            issue_date = date.today().replace(day=1)

        # Duplicate check
        existing = models.TimeInvoice.objects.filter(
            contract=contract,
            issue_date__year=issue_date.year,
            issue_date__month=issue_date.month,
        ).first()
        if existing:
            messages.warning(
                request,
                f"Contract for {contract.buyer.name} already has invoice "
                f"{existing.series_number} for this month.",
            )
            return redirect("billing-todo")

        with transaction.atomic():
            invoice = _create_draft_invoice(contract, issue_date, registry)

        messages.success(request, f"Draft {invoice.series_number} created.")
        return redirect(
            reverse(
                "registry-invoice-detail",
                kwargs={"registry_id": registry_id, "pk": invoice.pk},
            )
        )


class BillingTodoView(LoginRequiredMixin, TemplateView):
    """Monthly billing todo — shows which contracts need invoicing."""

    template_name = "billing_todo.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Determine target month
        try:
            target_year = int(self.request.GET.get("year", date.today().year))
            target_month = int(self.request.GET.get("month", date.today().month))
        except (ValueError, TypeError):
            target_year = date.today().year
            target_month = date.today().month

        # Compute prev/next month for navigation
        first = date(target_year, target_month, 1)
        prev_date = (first - timedelta(days=1)).replace(day=1)
        if target_month == 12:
            next_date = date(target_year + 1, 1, 1)
        else:
            next_date = date(target_year, target_month + 1, 1)

        # Fetch active monthly contracts with invoice status for target month
        registries = (
            user.registries.prefetch_related("seller", "contracts__buyer")
            .order_by("display_name")
        )

        todo_registries = []
        total_pending = 0
        total_draft = 0
        total_published = 0

        for registry in registries:
            contracts = registry.contracts.filter(
                is_active=True,
                unit=models.InvoicingUnits.MONTHLY,
            ).select_related("buyer")

            items = []
            for contract in contracts:
                existing = models.TimeInvoice.objects.filter(
                    contract=contract,
                    issue_date__year=target_year,
                    issue_date__month=target_month,
                ).first()

                if existing:
                    if existing.status == models.InvoiceStatus.DRAFT:
                        status = "draft"
                        total_draft += 1
                    elif existing.status == models.InvoiceStatus.PUBLISHED:
                        status = "published"
                        total_published += 1
                    else:
                        status = "storno"
                else:
                    status = "pending"
                    total_pending += 1

                items.append({
                    "contract": contract,
                    "status": status,
                    "existing_invoice": existing,
                })

            if items:
                todo_registries.append({
                    "registry": registry,
                    "items": items,
                })

        # Format month name
        month_name = f"{calendar.month_name[target_month]} {target_year}"

        context.update({
            "target_year": target_year,
            "target_month": target_month,
            "month_name": month_name,
            "prev_year": prev_date.year,
            "prev_month": prev_date.month,
            "next_year": next_date.year,
            "next_month": next_date.month,
            "todo_registries": todo_registries,
            "summary": {
                "pending": total_pending,
                "draft": total_draft,
                "published": total_published,
            },
        })
        return context


class BatchDraftView(LoginRequiredMixin, View):
    """Batch-create draft invoices for selected contracts."""

    def post(self, request):
        contract_ids = request.POST.getlist("contract_ids")
        if not contract_ids:
            messages.warning(request, "No contracts selected.")
            return redirect("billing-todo")

        try:
            target_year = int(request.POST.get("target_year", date.today().year))
            target_month = int(request.POST.get("target_month", date.today().month))
            issue_date = date(target_year, target_month, 1)
        except (ValueError, TypeError):
            issue_date = date.today().replace(day=1)

        # Fetch contracts owned by this user, active, monthly
        contracts = (
            models.ServiceContract.objects.filter(
                pk__in=contract_ids,
                registry__user=request.user,
                is_active=True,
                unit=models.InvoicingUnits.MONTHLY,
            )
            .select_related("buyer", "registry")
            .order_by("registry__display_name", "pk")
        )

        created = []
        skipped = []

        with transaction.atomic():
            for contract in contracts:
                # Duplicate check
                existing = models.TimeInvoice.objects.filter(
                    contract=contract,
                    issue_date__year=issue_date.year,
                    issue_date__month=issue_date.month,
                ).exists()
                if existing:
                    skipped.append(contract.buyer.name)
                    continue

                invoice = _create_draft_invoice(contract, issue_date, contract.registry)
                created.append(invoice)

        if created:
            names = ", ".join(inv.series_number for inv in created)
            messages.success(request, f"Created {len(created)} draft(s): {names}")
        if skipped:
            messages.warning(
                request,
                f"Skipped {len(skipped)} already invoiced: {', '.join(skipped)}",
            )

        return redirect(
            f"{reverse('billing-todo')}?year={target_year}&month={target_month}"
        )
