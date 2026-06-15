import io
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from . import models


class ArchiveRegistryTests(TestCase):
    """Covers the registry archive (soft-delete) capability end to end."""

    def setUp(self):
        self.user = models.MicroUser.objects.create_user(
            email="owner@example.com", password="pw", first_name="O", last_name="Wner"
        )
        self.other = models.MicroUser.objects.create_user(
            email="intruder@example.com", password="pw", first_name="In", last_name="Truder"
        )

    # --- builders -------------------------------------------------------

    def make_fiscal(self, name="Entity"):
        return models.FiscalEntity.objects.create(
            name=name,
            owner_fullname="Owner Name",
            registration_id="J1/23/45",
            fiscal_code="RO123",
            address="Street 1",
            country="RO",
            bank_account="RO00BANK0000",
            bank_name="Some Bank",
        )

    def make_registry(self, user=None, archived=False, display_name="Main"):
        registry = models.MicroRegistry.objects.create(
            user=user or self.user,
            seller=self.make_fiscal("Seller"),
            display_name=display_name,
            invoice_series="INV",
            next_invoice_no=1,
            include_vat=0,
        )
        if archived:
            registry.archive()
        return registry

    def make_contract(self, registry):
        return models.ServiceContract.objects.create(
            buyer=self.make_fiscal("Buyer"),
            registry=registry,
            registration_no="C-1",
            registration_date=date(2024, 1, 1),
            currency="ron",
            unit="hr",
            unit_rate=Decimal("100"),
            invoicing_currency="ron",
            invoicing_description="work",
        )

    def make_invoice(self, registry, number=1, unit_rate="100", quantity=1):
        contract = self.make_contract(registry)
        return models.TimeInvoice.objects.create(
            registry=registry,
            seller=registry.seller,
            buyer=contract.buyer,
            contract=contract,
            series=registry.invoice_series,
            number=number,
            status=models.InvoiceStatus.PUBLISHED,
            currency="ron",
            unit="hr",
            unit_rate=Decimal(unit_rate),
            issue_date=date(2024, 6, 1),
            quantity=quantity,
        )

    # --- model layer ----------------------------------------------------

    def test_archive_unarchive_toggles_state_and_querysets(self):
        registry = self.make_registry()
        self.assertFalse(registry.is_archived)
        self.assertIn(registry, models.MicroRegistry.objects.active())
        self.assertNotIn(registry, models.MicroRegistry.objects.archived())

        registry.archive()
        registry.refresh_from_db()
        self.assertTrue(registry.is_archived)
        self.assertIsNotNone(registry.archived_at)
        self.assertIn(registry, models.MicroRegistry.objects.archived())
        self.assertNotIn(registry, models.MicroRegistry.objects.active())

        registry.unarchive()
        registry.refresh_from_db()
        self.assertFalse(registry.is_archived)
        self.assertIsNone(registry.archived_at)
        self.assertIn(registry, models.MicroRegistry.objects.active())

    def test_has_business_data(self):
        empty = self.make_registry(display_name="Empty")
        self.assertFalse(empty.has_business_data)
        used = self.make_registry(display_name="Used")
        self.make_invoice(used)
        self.assertTrue(used.has_business_data)

    # --- dashboard ------------------------------------------------------

    def test_home_separates_active_from_archived(self):
        active = self.make_registry(display_name="Active one")
        archived = self.make_registry(display_name="Archived one", archived=True)
        self.client.force_login(self.user)

        resp = self.client.get(reverse("home"))

        self.assertEqual(resp.status_code, 200)
        self.assertIn(active, resp.context["registries"])
        self.assertNotIn(archived, resp.context["registries"])
        self.assertIn(archived, resp.context["archived_registries"])
        self.assertNotIn(active, resp.context["archived_registries"])

    # --- gating new contracts / invoices --------------------------------

    def test_archived_registry_blocks_new_contract(self):
        registry = self.make_registry(archived=True)
        self.client.force_login(self.user)
        url = reverse("registry-contract-add", kwargs={"registry_id": registry.id})

        self.assertRedirects(self.client.get(url), reverse("home"))
        self.client.post(url, {})
        self.assertEqual(models.ServiceContract.objects.filter(registry=registry).count(), 0)

    def test_archived_registry_blocks_new_invoice(self):
        registry = self.make_registry(archived=True)
        self.client.force_login(self.user)
        url = reverse("registry-invoice-add", kwargs={"registry_id": registry.id})

        self.assertRedirects(self.client.get(url), reverse("home"))
        self.client.post(url, {})
        self.assertEqual(models.TimeInvoice.objects.filter(registry=registry).count(), 0)

    def test_active_registry_allows_contract_form(self):
        registry = self.make_registry()
        self.client.force_login(self.user)
        url = reverse("registry-contract-add", kwargs={"registry_id": registry.id})
        self.assertEqual(self.client.get(url).status_code, 200)

    # --- delete vs archive ----------------------------------------------

    def test_delete_blocked_when_registry_has_history(self):
        registry = self.make_registry()
        self.make_invoice(registry)
        self.assertTrue(registry.has_business_data)
        self.client.force_login(self.user)
        url = reverse("registry-delete", kwargs={"pk": registry.id})

        self.assertRedirects(self.client.get(url), reverse("home"))
        self.client.post(url)
        self.assertTrue(models.MicroRegistry.objects.filter(pk=registry.id).exists())

    def test_delete_allowed_when_registry_empty(self):
        registry = self.make_registry()
        self.assertFalse(registry.has_business_data)
        self.client.force_login(self.user)
        url = reverse("registry-delete", kwargs={"pk": registry.id})

        self.assertEqual(self.client.get(url).status_code, 200)  # confirm page renders
        self.assertRedirects(self.client.post(url), reverse("home"))
        self.assertFalse(models.MicroRegistry.objects.filter(pk=registry.id).exists())

    # --- archive / restore actions --------------------------------------

    def test_archive_view_post_archives_and_get_not_allowed(self):
        registry = self.make_registry()
        self.client.force_login(self.user)
        url = reverse("registry-archive", kwargs={"pk": registry.id})

        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertRedirects(self.client.post(url), reverse("home"))
        registry.refresh_from_db()
        self.assertTrue(registry.is_archived)

    def test_restore_view_unarchives(self):
        registry = self.make_registry(archived=True)
        self.client.force_login(self.user)
        resp = self.client.post(reverse("registry-restore", kwargs={"pk": registry.id}))

        self.assertRedirects(resp, reverse("home"))
        registry.refresh_from_db()
        self.assertFalse(registry.is_archived)

    def test_actions_are_scoped_to_owner(self):
        registry = self.make_registry(user=self.user)
        self.client.force_login(self.other)

        self.assertEqual(
            self.client.post(reverse("registry-archive", kwargs={"pk": registry.id})).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("registry-restore", kwargs={"pk": registry.id})).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("registry-delete", kwargs={"pk": registry.id})).status_code,
            404,
        )

    # --- reports --------------------------------------------------------

    def test_report_excludes_archived_unless_requested(self):
        active = self.make_registry(display_name="A")
        self.make_invoice(active, number=1, unit_rate="100", quantity=1)
        archived = self.make_registry(display_name="B", archived=True)
        self.make_invoice(archived, number=1, unit_rate="100", quantity=1)
        self.client.force_login(self.user)

        def grand_total(resp):
            return sum(year["total"] for year in resp.context["totals"].values())

        default = self.client.get(reverse("report"))
        self.assertFalse(default.context["include_archived"])
        self.assertEqual(grand_total(default), Decimal("100"))

        included = self.client.get(reverse("report"), {"include_archived": "1"})
        self.assertTrue(included.context["include_archived"])
        self.assertEqual(grand_total(included), Decimal("200"))

        # "0" must NOT be treated as truthy
        zero = self.client.get(reverse("report"), {"include_archived": "0"})
        self.assertFalse(zero.context["include_archived"])
        self.assertEqual(grand_total(zero), Decimal("100"))

    # --- history stays reachable after archiving ------------------------

    def test_archived_registry_history_still_accessible(self):
        registry = self.make_registry()
        invoice = self.make_invoice(registry)
        registry.archive()
        self.client.force_login(self.user)

        detail = self.client.get(
            reverse("registry-invoice-detail", kwargs={"registry_id": registry.id, "pk": invoice.id})
        )
        self.assertEqual(detail.status_code, 200)

        # the PDF view must still serve; mock the renderer so the test does not
        # depend on the wkhtmltopdf binary / system locale being present.
        with mock.patch(
            "microinvoicer.views.pdf_rendering.render_invoice",
            return_value=io.BytesIO(b"%PDF-1.4 test"),
        ):
            pdf = self.client.get(
                reverse(
                    "registry-invoice-print",
                    kwargs={"registry_id": registry.id, "pk": invoice.id},
                )
            )
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
