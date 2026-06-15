from datetime import date
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse

from . import models
from .models import InvoiceStatus
from .views import _billing_overview


def make_fiscal(name):
    return models.FiscalEntity.objects.create(
        name=name,
        owner_fullname=f"{name} Owner",
        registration_id="J00/000/2020",
        fiscal_code="RO0000000",
        address="Some street 1",
        country="RO",
        bank_account="RO00BANK0000000000000000",
        bank_name="Some Bank",
    )


class BillingFlowTests(TestCase):
    """End-to-end coverage of the draft -> publish lifecycle and the monthly
    billing worklist / batch-draft behaviour.

    NOTE: running these requires Django app startup, and
    ``MicroinvoicerConfig.ready()`` performs a live HTTP GET to bnr.ro. Run with
    that endpoint reachable, or stub ``requests.get`` before ``django.setup()``.
    """

    def setUp(self):
        self.user = models.MicroUser.objects.create_user(
            email="seller@example.com", password="pw", first_name="Sel", last_name="Ler"
        )
        self.seller = make_fiscal("Seller SRL")
        self.registry = models.MicroRegistry.objects.create(
            user=self.user,
            seller=self.seller,
            display_name="Main",
            invoice_series="ABC",
            next_invoice_no=1,
            include_vat=0,
        )
        self.buyer = make_fiscal("Buyer SRL")
        self.contract = self._contract("C-1", self.buyer)
        self.client = Client()
        self.client.force_login(self.user)

    # -- helpers ---------------------------------------------------------

    def _contract(self, registration_no, buyer, unit_rate="1000.00"):
        return models.ServiceContract.objects.create(
            buyer=buyer,
            registry=self.registry,
            registration_no=registration_no,
            registration_date=date(2020, 1, 1),
            currency="ron",
            unit="mo",
            unit_rate=Decimal(unit_rate),
            invoicing_currency="ron",
            invoicing_description="Services for {{ this_month }}",
        )

    def make_draft(self, contract, when, status=InvoiceStatus.DRAFT, number=None):
        return models.TimeInvoice.objects.create(
            registry=contract.registry,
            seller=contract.registry.seller,
            buyer=contract.buyer,
            contract=contract,
            series=contract.registry.invoice_series,
            number=number,
            status=status,
            description="x",
            currency=contract.invoicing_currency,
            conversion_rate=Decimal("1"),
            unit=contract.unit,
            unit_rate=contract.unit_rate,
            issue_date=when,
            quantity=1,
            include_vat=0,
        )

    def add_url(self):
        return reverse("registry-invoice-add", kwargs={"registry_id": self.registry.id})

    def publish_url(self, invoice):
        return reverse(
            "registry-invoice-publish",
            kwargs={"registry_id": self.registry.id, "pk": invoice.id},
        )

    # -- the single-invoice flow is now draft-first ----------------------

    def test_single_create_makes_unnumbered_draft(self):
        resp = self.client.post(
            self.add_url(),
            data={"contract": self.contract.id, "issue_date": date.today().isoformat(), "quantity": 1},
        )
        self.assertEqual(resp.status_code, 302)
        invoice = models.TimeInvoice.objects.get()
        self.assertEqual(invoice.status, InvoiceStatus.DRAFT)
        self.assertIsNone(invoice.number)
        self.registry.refresh_from_db()
        self.assertEqual(self.registry.next_invoice_no, 1)  # not consumed by a draft

    # -- publishing allocates the official number ------------------------

    def test_publish_allocates_sequential_numbers(self):
        d1 = self.make_draft(self.contract, date.today())
        d2 = self.make_draft(self.contract, date.today())
        for expected, draft in [(1, d1), (2, d2)]:
            resp = self.client.post(self.publish_url(draft))
            self.assertEqual(resp.status_code, 302)
            draft.refresh_from_db()
            self.assertEqual(draft.status, InvoiceStatus.PUBLISHED)
            self.assertEqual(draft.number, expected)
        self.registry.refresh_from_db()
        self.assertEqual(self.registry.next_invoice_no, 3)

    def test_publish_is_idempotent(self):
        d1 = self.make_draft(self.contract, date.today())
        self.client.post(self.publish_url(d1))
        self.client.post(self.publish_url(d1))  # second publish must be a no-op
        d1.refresh_from_db()
        self.registry.refresh_from_db()
        self.assertEqual(d1.number, 1)
        self.assertEqual(self.registry.next_invoice_no, 2)

    # -- worklist classification -----------------------------------------

    def test_worklist_overview_classifies_states(self):
        month = date.today().replace(day=1)

        groups, summary = _billing_overview(self.user, month)
        self.assertEqual(summary, {"pending": 1, "drafts": 0, "published": 0})
        self.assertEqual(groups[0]["rows"][0]["state"], "pending")

        draft = self.make_draft(self.contract, date.today())
        _, summary = _billing_overview(self.user, month)
        self.assertEqual((summary["pending"], summary["drafts"]), (0, 1))

        draft.status = InvoiceStatus.PUBLISHED
        draft.number = 1
        draft.save()
        groups, summary = _billing_overview(self.user, month)
        self.assertEqual(summary["published"], 1)
        self.assertEqual(groups[0]["rows"][0]["state"], "published")

    def test_worklist_flags_duplicates(self):
        month = date.today().replace(day=1)
        self.make_draft(self.contract, date.today())
        self.make_draft(self.contract, date.today())
        groups, _ = _billing_overview(self.user, month)
        self.assertTrue(groups[0]["rows"][0]["duplicate"])

    # -- batch draft creation --------------------------------------------

    def test_batch_create_skips_already_billed(self):
        contract2 = self._contract("C-2", make_fiscal("Buyer Two SRL"), unit_rate="500.00")
        self.make_draft(self.contract, date.today())  # already billed -> skip

        resp = self.client.post(
            reverse("billing-worklist-batch-create"),
            data={
                "month": date.today().strftime("%Y-%m"),
                "contract_ids": [self.contract.id, contract2.id],
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("created=1", resp.url)
        self.assertIn("skipped=1", resp.url)

        c2 = models.TimeInvoice.objects.filter(contract=contract2)
        self.assertEqual(c2.count(), 1)
        self.assertEqual(c2.first().status, InvoiceStatus.DRAFT)
        self.assertIsNone(c2.first().number)
        self.assertEqual(models.TimeInvoice.objects.filter(contract=self.contract).count(), 1)
        self.registry.refresh_from_db()
        self.assertEqual(self.registry.next_invoice_no, 1)  # no numbers consumed

    def test_batch_rerun_is_idempotent(self):
        url = reverse("billing-worklist-batch-create")
        month = date.today().strftime("%Y-%m")
        first = self.client.post(url, data={"month": month, "contract_ids": [self.contract.id]})
        self.assertIn("created=1", first.url)
        second = self.client.post(url, data={"month": month, "contract_ids": [self.contract.id]})
        self.assertIn("created=0", second.url)
        self.assertIn("skipped=1", second.url)
        self.assertEqual(models.TimeInvoice.objects.filter(contract=self.contract).count(), 1)

    # -- delete must not corrupt the sequence ----------------------------

    def test_delete_draft_keeps_counter(self):
        d1 = self.make_draft(self.contract, date.today())
        resp = self.client.post(
            reverse("registry-invoice-delete", kwargs={"registry_id": self.registry.id, "pk": d1.id})
        )
        self.assertEqual(resp.status_code, 302)
        self.registry.refresh_from_db()
        self.assertEqual(self.registry.next_invoice_no, 1)  # never decremented for a draft
        self.assertFalse(models.TimeInvoice.objects.filter(pk=d1.id).exists())

    # -- workbench counters ----------------------------------------------

    def test_home_shows_billing_summary(self):
        self.make_draft(self.contract, date.today())
        resp = self.client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["billing_summary"]["drafts"], 1)
        self.assertEqual(resp.context["billing_summary"]["pending"], 0)
