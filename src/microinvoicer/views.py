"""How about now."""
from datetime import date, timedelta, datetime
from django.http import FileResponse
from django.urls import reverse, reverse_lazy
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

from . import forms, models, pdf_rendering
from .temporary_locale import TemporaryLocale
from django.contrib import messages
from django.shortcuts import redirect, get_object_or_404
from django.forms import inlineformset_factory


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

    def get_success_url(self):
        """Redirect to invoice detail page after creation"""
        return reverse(
            "registry-invoice-detail",
            kwargs={
                "registry_id": self.kwargs["registry_id"],
                "pk": self.object.pk,
            },
        )


class TimeInvoiceDeleteView(MicroFormMixin, DeleteView):
    model = models.TimeInvoice
    form_title = "Throwing away invoice"
    template_name = "confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        invoice = self.get_object()
        if not invoice.is_draft:
            messages.error(
                request,
                "Only draft invoices can be deleted. Revert to draft first.",
            )
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )
        return super().dispatch(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        invoice = self.get_object()
        invoice.registry.next_invoice_no -= 1
        invoice.registry.save()
        return super().delete(request, *args, **kwargs)


class TimeInvoiceDetailView(LoginRequiredMixin, DetailView):
    model = models.TimeInvoice
    template_name = "invoice_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invoice = self.object
        entries = invoice.timesheet_entries.all()
        entries_total = sum((e.hours for e in entries), Decimal(0))

        context["timesheet_entries"] = entries
        context["entries_total"] = entries_total
        context["entries_diff"] = invoice.quantity - entries_total
        context["can_edit_timesheet"] = invoice.is_draft
        context["can_publish"] = (
            invoice.is_draft and entries_total == invoice.quantity and entries.exists()
        )
        context["can_revert"] = invoice.is_published
        context["is_draft"] = invoice.is_draft
        context["is_published"] = invoice.is_published

        return context


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
    """Generate timesheet as PDF file from real entries"""

    model = models.TimeInvoice
    response_class = FileResponse

    def render_to_response(self, context, **response_kwargs):
        """Returns content of generated pdf"""
        invoice = context["object"]
        entries = invoice.timesheet_entries.all()

        if not entries.exists():
            messages.error(
                self.request, "Please add timesheet entries before downloading the PDF."
            )
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        content = pdf_rendering.render_timesheet(invoice, entries)
        response = FileResponse(
            content,
            filename=f"{invoice.series_number}-timesheet.pdf",
            as_attachment=True,
            content_type="application/pdf",
        )
        return response


class TimesheetEntryEditView(LoginRequiredMixin, TemplateView):
    """Edit timesheet entries for a draft invoice"""

    template_name = "timesheet_edit.html"

    def get_invoice(self):
        return get_object_or_404(
            models.TimeInvoice,
            pk=self.kwargs["pk"],
            registry_id=self.kwargs["registry_id"],
        )

    def get(self, request, *args, **kwargs):
        invoice = self.get_invoice()
        if not invoice.is_draft:
            messages.warning(request, "Timesheet entries can only be edited for draft invoices.")
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        formset = forms.TimesheetEntryFormSet(instance=invoice)

        # Pre-fill from templates if requested and no entries exist yet
        from_template = request.GET.get("from_template")
        if from_template and not invoice.timesheet_entries.exists():
            templates = invoice.contract.timesheet_templates.all()
            if templates.exists():
                initial = [
                    {"project": t.project, "task": t.task, "date": invoice.issue_date, "hours": 1}
                    for t in templates
                ]
                # Dynamically create formset with enough extra forms
                DynamicFormSet = inlineformset_factory(
                    models.TimeInvoice,
                    models.TimesheetEntry,
                    form=forms.TimesheetEntryForm,
                    extra=max(len(initial), 5),
                    can_delete=True,
                )
                formset = DynamicFormSet(instance=invoice, initial=initial)

        return self._render(invoice, formset)

    def post(self, request, *args, **kwargs):
        invoice = self.get_invoice()
        if not invoice.is_draft:
            messages.warning(request, "Timesheet entries can only be edited for draft invoices.")
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        formset = forms.TimesheetEntryFormSet(request.POST, instance=invoice)
        if formset.is_valid():
            formset.save()
            messages.success(request, "Timesheet entries saved.")
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        return self._render(invoice, formset)

    def _render(self, invoice, formset):
        entries = invoice.timesheet_entries.all()
        entries_total = sum((e.hours for e in entries), Decimal(0))
        templates = invoice.contract.timesheet_templates.all()

        context = {
            "invoice": invoice,
            "formset": formset,
            "entries_total": entries_total,
            "entries_diff": invoice.quantity - entries_total,
            "has_templates": templates.exists(),
            "has_entries": entries.exists(),
        }
        return self.render_to_response(context)


class TimeInvoicePublishView(LoginRequiredMixin, TemplateView):
    """Publish a draft invoice after validating timesheet entries"""

    def post(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            models.TimeInvoice,
            pk=kwargs["pk"],
            registry_id=kwargs["registry_id"],
        )

        if not invoice.is_draft:
            messages.info(request, "Invoice is already published.")
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        entries = invoice.timesheet_entries.all()
        entries_total = sum((e.hours for e in entries), Decimal(0))

        if entries_total != invoice.quantity:
            messages.error(
                request,
                f"Cannot publish: timesheet entries total {entries_total} "
                f"but invoice quantity is {invoice.quantity}. "
                "They must match exactly.",
            )
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        if not entries.exists():
            messages.error(request, "Cannot publish: please add timesheet entries first.")
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        invoice.status = models.InvoiceStatus.PUBLISHED
        invoice.save()
        messages.success(request, "Invoice published successfully.")
        return redirect(
            "registry-invoice-detail",
            registry_id=invoice.registry.id,
            pk=invoice.id,
        )


class TimeInvoiceRevertView(LoginRequiredMixin, TemplateView):
    """Revert a published invoice back to draft"""

    def post(self, request, *args, **kwargs):
        invoice = get_object_or_404(
            models.TimeInvoice,
            pk=kwargs["pk"],
            registry_id=kwargs["registry_id"],
        )

        if not invoice.is_published:
            messages.info(request, "Only published invoices can be reverted to draft.")
            return redirect(
                "registry-invoice-detail",
                registry_id=invoice.registry.id,
                pk=invoice.id,
            )

        invoice.status = models.InvoiceStatus.DRAFT
        invoice.save()
        messages.success(request, "Invoice reverted to draft. You can now edit timesheet entries.")
        return redirect(
            "registry-invoice-detail",
            registry_id=invoice.registry.id,
            pk=invoice.id,
        )


class TimesheetTemplateView(MicroFormMixin, UpdateView):
    """Manage timesheet templates for a contract"""

    model = models.ServiceContract
    form_title = "Timesheet Templates"
    template_name = "timesheet_templates.html"
    fields = []  # We only use the formset

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        formset = forms.TimesheetTemplateFormSet(instance=self.object)
        return self._render(formset)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        formset = forms.TimesheetTemplateFormSet(request.POST, instance=self.object)
        if formset.is_valid():
            formset.save()
            messages.success(request, "Templates saved.")
            return self._render(forms.TimesheetTemplateFormSet(instance=self.object))
        return self._render(formset)

    def _render(self, formset):
        context = {
            "contract": self.object,
            "formset": formset,
            "form_title": self.form_title,
        }
        return self.render_to_response(context)
