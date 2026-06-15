import io
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from . import forms, models


def make_entity(name):
    return models.FiscalEntity.objects.create(
        name=name,
        owner_fullname=f"{name} Owner",
        registration_id="J00/1/2000",
        fiscal_code="RO123",
        address="1 Some Street",
        country="IE",
        bank_account="IE00 0000",
        bank_name="Some Bank",
    )


class TimesheetTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="seller@example.com", password="pw", first_name="Sel", last_name="Ler"
        )
        self.seller = make_entity("Seller SRL")
        self.buyer = make_entity("Buyer Ltd")
        self.registry = models.MicroRegistry.objects.create(
            user=self.user,
            seller=self.seller,
            display_name="Reg",
            invoice_series="AB",
            next_invoice_no=1,
            include_vat=0,
        )
        self.contract = models.ServiceContract.objects.create(
            buyer=self.buyer,
            registry=self.registry,
            registration_no="C-1",
            registration_date=date(2026, 1, 1),
            currency=models.AvailableCurrencies.EUR,
            unit=models.InvoicingUnits.HOURLY,
            unit_rate=Decimal("50"),
            invoicing_currency=models.AvailableCurrencies.EUR,
            invoicing_description="services",
            default_project="Acme Portal",
            task_presets="Backend work\nCode review\n\nSupport",
        )
        self.client.force_login(self.user)

    def make_invoice(self, status=models.InvoiceStatus.DRAFT, quantity=20, number=1):
        return models.TimeInvoice.objects.create(
            registry=self.registry,
            seller=self.seller,
            buyer=self.buyer,
            contract=self.contract,
            series="AB",
            number=number,
            status=status,
            description="services",
            currency=models.AvailableCurrencies.EUR,
            conversion_rate=None,
            unit=models.InvoicingUnits.HOURLY,
            unit_rate=Decimal("50"),
            issue_date=date(2026, 6, 1),
            quantity=quantity,
            include_vat=0,
        )

    def add_entries(self, invoice, rows):
        for work_date, project, activity, hours in rows:
            models.TimesheetEntry.objects.create(
                invoice=invoice,
                work_date=work_date,
                project=project,
                activity=activity,
                hours=Decimal(str(hours)),
            )

    def edit_url(self, invoice):
        return reverse(
            "registry-invoice-timesheet-edit",
            kwargs={"registry_id": invoice.registry_id, "pk": invoice.pk},
        )

    def ts_post_data(self, invoice, rows):
        """Build inline-formset POST data for the given rows (list of dicts)."""
        prefix = forms.TimesheetEntryFormSet(instance=invoice).prefix
        data = {
            f"{prefix}-TOTAL_FORMS": str(len(rows)),
            f"{prefix}-INITIAL_FORMS": str(invoice.timesheet_entries.count()),
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        for i, row in enumerate(rows):
            data[f"{prefix}-{i}-work_date"] = row["work_date"]
            data[f"{prefix}-{i}-project"] = row.get("project", "")
            data[f"{prefix}-{i}-activity"] = row["activity"]
            data[f"{prefix}-{i}-hours"] = str(row["hours"])
            if row.get("id"):
                data[f"{prefix}-{i}-id"] = str(row["id"])
            if row.get("DELETE"):
                data[f"{prefix}-{i}-DELETE"] = "on"
        return data

    # --- requirement 6: new invoices start as drafts -----------------------
    def test_invoice_created_as_draft(self):
        url = reverse("registry-invoice-add", kwargs={"registry_id": self.registry.pk})
        resp = self.client.post(
            url,
            {
                "contract": self.contract.pk,
                "issue_date": "2026-06-15",
                "quantity": "20",
                "conversion_rate": "",
                "attached_description": "",
                "attached_cost": "",
                "override_description": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        invoice = models.TimeInvoice.objects.get(registry=self.registry)
        self.assertEqual(invoice.status, models.InvoiceStatus.DRAFT)
        self.assertTrue(invoice.is_draft)

    # --- requirement 3: total must reconcile with invoice quantity ----------
    def test_save_rejects_unbalanced_timesheet(self):
        invoice = self.make_invoice(quantity=20)
        resp = self.client.post(
            self.edit_url(invoice),
            self.ts_post_data(
                invoice,
                [{"work_date": "2026-06-01", "activity": "Work", "hours": 17}],
            ),
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered, not redirected
        self.assertEqual(invoice.timesheet_entries.count(), 0)  # nothing saved
        self.assertContains(resp, "add up to exactly 20", status_code=200)

    def test_save_accepts_balanced_timesheet(self):
        invoice = self.make_invoice(quantity=20)
        resp = self.client.post(
            self.edit_url(invoice),
            self.ts_post_data(
                invoice,
                [
                    {"work_date": "2026-06-01", "project": "Acme", "activity": "Build", "hours": 12},
                    {"work_date": "2026-06-02", "project": "Acme", "activity": "Test", "hours": 8},
                ],
            ),
        )
        self.assertEqual(resp.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.timesheet_entries.count(), 2)
        self.assertEqual(invoice.timesheet_total_hours, Decimal("20"))
        self.assertTrue(invoice.timesheet_reconciled)

    def test_save_rejects_empty_timesheet(self):
        invoice = self.make_invoice(quantity=20)
        resp = self.client.post(self.edit_url(invoice), self.ts_post_data(invoice, []))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "at least one timesheet entry")

    # --- requirement 6: publish gated on a reconciled timesheet -------------
    def publish_url(self, invoice):
        return reverse(
            "registry-invoice-publish",
            kwargs={"registry_id": invoice.registry_id, "pk": invoice.pk},
        )

    def test_publish_blocked_without_entries(self):
        invoice = self.make_invoice(quantity=20)
        resp = self.client.post(self.publish_url(invoice))
        self.assertEqual(resp.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, models.InvoiceStatus.DRAFT)

    def test_publish_blocked_when_unbalanced(self):
        invoice = self.make_invoice(quantity=20)
        self.add_entries(invoice, [(date(2026, 6, 1), "P", "Work", 17)])
        resp = self.client.post(self.publish_url(invoice))
        self.assertEqual(resp.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, models.InvoiceStatus.DRAFT)

    def test_publish_succeeds_when_balanced(self):
        invoice = self.make_invoice(quantity=20)
        self.add_entries(invoice, [(date(2026, 6, 1), "P", "Work", 20)])
        resp = self.client.post(self.publish_url(invoice))
        self.assertEqual(resp.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, models.InvoiceStatus.PUBLISHED)

    # --- requirement 6: published timesheet is read-only --------------------
    def test_published_timesheet_edit_blocked_on_get(self):
        invoice = self.make_invoice(status=models.InvoiceStatus.PUBLISHED, quantity=20)
        resp = self.client.get(self.edit_url(invoice))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("detail", resp.url)

    def test_published_timesheet_edit_blocked_on_post(self):
        invoice = self.make_invoice(status=models.InvoiceStatus.PUBLISHED, quantity=20)
        resp = self.client.post(
            self.edit_url(invoice),
            self.ts_post_data(
                invoice, [{"work_date": "2026-06-01", "activity": "Work", "hours": 20}]
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(invoice.timesheet_entries.count(), 0)  # unchanged

    # --- requirement 7: PDF is built from persisted entries -----------------
    @patch("microinvoicer.views.pdf_rendering.render_timesheet")
    def test_timesheet_pdf_uses_saved_entries(self, mock_render):
        mock_render.return_value = io.BytesIO(b"%PDF-1.4 fake")
        invoice = self.make_invoice(quantity=20)
        # add out of date order to confirm the view emits them sorted
        self.add_entries(
            invoice,
            [
                (date(2026, 6, 3), "Acme Portal", "Second", 8),
                (date(2026, 6, 1), "Acme Portal", "First", 12),
            ],
        )
        url = reverse(
            "registry-invoice-timesheet",
            kwargs={"registry_id": invoice.registry_id, "pk": invoice.pk},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once()
        passed_invoice, payload = mock_render.call_args.args
        self.assertEqual(passed_invoice.pk, invoice.pk)
        tasks = payload["tasks"]
        self.assertEqual([t["name"] for t in tasks], ["First", "Second"])
        self.assertEqual([t["date"] for t in tasks], [date(2026, 6, 1), date(2026, 6, 3)])
        self.assertEqual(tasks[0]["project"], "Acme Portal")
        self.assertEqual(sum(t["duration"] for t in tasks), Decimal("20"))

    @patch("microinvoicer.views.pdf_rendering.render_timesheet")
    def test_timesheet_pdf_blocked_when_unbalanced(self, mock_render):
        invoice = self.make_invoice(quantity=20)
        self.add_entries(invoice, [(date(2026, 6, 1), "P", "Work", 17)])
        url = reverse(
            "registry-invoice-timesheet",
            kwargs={"registry_id": invoice.registry_id, "pk": invoice.pk},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        mock_render.assert_not_called()

    # --- requirement 4: per-contract reuse ----------------------------------
    def test_contract_preset_lines(self):
        self.assertEqual(
            self.contract.preset_lines(), ["Backend work", "Code review", "Support"]
        )

    def test_edit_page_seeds_default_project_and_presets(self):
        invoice = self.make_invoice(quantity=20)
        resp = self.client.get(self.edit_url(invoice))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="Acme Portal"')  # seeded default project
        self.assertContains(resp, "Backend work")  # preset in the datalist

    # --- requirement 5: detail page previews the timesheet ------------------
    def test_detail_page_shows_timesheet_preview(self):
        invoice = self.make_invoice(quantity=20)
        self.add_entries(invoice, [(date(2026, 6, 1), "Acme", "Build the thing", 20)])
        url = reverse(
            "registry-invoice-detail",
            kwargs={"registry_id": invoice.registry_id, "pk": invoice.pk},
        )
        resp = self.client.get(url)
        self.assertContains(resp, "Build the thing")
        self.assertContains(resp, "Balanced")
        self.assertContains(resp, "Download Timesheet PDF")
