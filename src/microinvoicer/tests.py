from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from . import models


class InvoiceLifecycleTests(TestCase):
    def setUp(self):
        self.user = models.MicroUser.objects.create_user(
            email="seller@example.com",
            password="secret-pass",
            first_name="Sel",
            last_name="Ler",
        )

        def make_entity(name):
            return models.FiscalEntity.objects.create(
                name=name,
                owner_fullname=f"{name} Owner",
                registration_id="J00/000/0000",
                fiscal_code="RO000000",
                address="Some street 1",
                country="RO",
                bank_account="RO00BANK0000000000000000",
                bank_name="Some Bank",
            )

        self.seller = make_entity("Seller Ltd")
        self.buyer = make_entity("Buyer Ltd")
        self.registry = models.MicroRegistry.objects.create(
            user=self.user,
            seller=self.seller,
            display_name="Main",
            invoice_series="INV",
            next_invoice_no=1,
            include_vat=0,
        )
        self.contract = models.ServiceContract.objects.create(
            buyer=self.buyer,
            registry=self.registry,
            registration_no="C-1",
            registration_date="2026-01-01",
            currency=models.AvailableCurrencies.EUR,
            unit=models.InvoicingUnits.MONTHLY,
            unit_rate=Decimal("100.00"),
            invoicing_currency=models.AvailableCurrencies.EUR,
            invoicing_description="Services",
        )
        self.client.force_login(self.user)

    # ----- helpers -------------------------------------------------------
    def _create_draft(self, quantity=3):
        url = reverse("registry-invoice-add", kwargs={"registry_id": self.registry.id})
        resp = self.client.post(
            url,
            {"contract": self.contract.id, "issue_date": "2026-06-01", "quantity": quantity},
        )
        self.assertEqual(resp.status_code, 302)
        return models.TimeInvoice.objects.latest("id")

    def _publish(self, invoice):
        return self.client.post(
            reverse(
                "registry-invoice-publish",
                kwargs={"registry_id": self.registry.id, "pk": invoice.id},
            )
        )

    def _storno(self, invoice):
        return self.client.post(
            reverse(
                "registry-invoice-storno",
                kwargs={"registry_id": self.registry.id, "pk": invoice.id},
            )
        )

    def _reload(self, invoice):
        return models.TimeInvoice.objects.get(pk=invoice.pk)

    # ----- tests ---------------------------------------------------------
    def test_new_invoice_is_unnumbered_draft(self):
        invoice = self._create_draft()
        self.assertEqual(invoice.status, models.InvoiceStatus.DRAFT)
        self.assertIsNone(invoice.number)
        self.registry.refresh_from_db()
        self.assertEqual(self.registry.next_invoice_no, 1)  # no number consumed

    def test_publish_assigns_number_gap_free(self):
        first = self._create_draft()
        self._publish(first)
        first = self._reload(first)
        self.registry.refresh_from_db()
        self.assertEqual(first.status, models.InvoiceStatus.PUBLISHED)
        self.assertEqual(first.number, 1)
        self.assertEqual(self.registry.next_invoice_no, 2)

        second = self._create_draft(quantity=2)
        self._publish(second)
        second = self._reload(second)
        self.registry.refresh_from_db()
        self.assertEqual(second.number, 2)
        self.assertEqual(self.registry.next_invoice_no, 3)

    def test_draft_can_be_edited(self):
        invoice = self._create_draft(quantity=3)
        resp = self.client.post(
            reverse(
                "registry-invoice-update",
                kwargs={"registry_id": self.registry.id, "pk": invoice.id},
            ),
            {"contract": self.contract.id, "issue_date": "2026-06-01", "quantity": 7},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._reload(invoice).quantity, 7)

    def test_published_invoice_cannot_be_edited(self):
        invoice = self._create_draft(quantity=3)
        self._publish(invoice)
        resp = self.client.post(
            reverse(
                "registry-invoice-update",
                kwargs={"registry_id": self.registry.id, "pk": invoice.id},
            ),
            {"contract": self.contract.id, "issue_date": "2026-06-01", "quantity": 99},
        )
        self.assertEqual(resp.status_code, 302)  # bounced by DraftOnlyMixin
        self.assertEqual(self._reload(invoice).quantity, 3)  # unchanged

    def test_only_draft_can_be_deleted(self):
        draft = self._create_draft()
        self.client.post(
            reverse(
                "registry-invoice-delete",
                kwargs={"registry_id": self.registry.id, "pk": draft.id},
            )
        )
        self.assertFalse(models.TimeInvoice.objects.filter(pk=draft.pk).exists())

        published = self._create_draft()
        self._publish(published)
        self.client.post(
            reverse(
                "registry-invoice-delete",
                kwargs={"registry_id": self.registry.id, "pk": published.id},
            )
        )
        self.assertTrue(models.TimeInvoice.objects.filter(pk=published.pk).exists())

    def test_storno_creates_linked_negative_document(self):
        invoice = self._create_draft(quantity=3)
        self._publish(invoice)
        self._storno(invoice)

        invoice = self._reload(invoice)
        self.assertEqual(invoice.status, models.InvoiceStatus.PUBLISHED)  # original intact
        self.assertTrue(invoice.is_reversed)

        storno = models.TimeInvoice.objects.get(status=models.InvoiceStatus.STORNO)
        self.assertEqual(storno.storno_of_id, invoice.id)
        self.assertEqual(storno.number, 2)  # its own series number
        self.assertEqual(storno.value, -invoice.value)
        self.assertLess(storno.value, 0)

    def test_double_storno_is_blocked(self):
        invoice = self._create_draft()
        self._publish(invoice)
        self._storno(invoice)
        self._storno(invoice)  # second attempt
        self.assertEqual(
            models.TimeInvoice.objects.filter(status=models.InvoiceStatus.STORNO).count(), 1
        )

    def test_cannot_storno_a_draft(self):
        draft = self._create_draft()
        self._storno(draft)
        self.assertEqual(
            models.TimeInvoice.objects.filter(status=models.InvoiceStatus.STORNO).count(), 0
        )

    def test_net_excludes_drafts_and_reflects_storno(self):
        published = self._create_draft(quantity=3)  # value 300
        self._publish(published)
        self._storno(published)  # -300
        self._create_draft(quantity=5)  # extra draft, must not count

        official = models.TimeInvoice.objects.filter(
            registry__user=self.user,
            status__in=[models.InvoiceStatus.PUBLISHED, models.InvoiceStatus.STORNO],
        )
        self.assertEqual(sum(i.value for i in official), Decimal(0))

        resp = self.client.get(reverse("home"))
        registry = next(r for r in resp.context["registries"] if r.id == self.registry.id)
        self.assertEqual(registry.net_total, Decimal(0))
        self.assertEqual(registry.draft_count, 1)
        self.assertEqual(registry.published_count, 1)
        self.assertEqual(registry.storno_count, 1)

    def test_draft_cannot_be_printed(self):
        draft = self._create_draft()
        resp = self.client.get(
            reverse(
                "registry-invoice-print",
                kwargs={"registry_id": self.registry.id, "pk": draft.id},
            )
        )
        self.assertEqual(resp.status_code, 403)
