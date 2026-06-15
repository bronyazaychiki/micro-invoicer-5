import django.db.models.deletion
from django.db import migrations, models


def populate_invoice_snapshots(apps, schema_editor):
    """Copy buyer/seller FiscalEntity data into snapshot fields."""
    TimeInvoice = apps.get_model("microinvoicer", "TimeInvoice")

    for invoice in TimeInvoice.objects.select_related("buyer", "seller").all():
        changed = False
        if invoice.buyer:
            invoice.buyer_snapshot_name = invoice.buyer.name
            invoice.buyer_snapshot_owner_fullname = invoice.buyer.owner_fullname
            invoice.buyer_snapshot_registration_id = invoice.buyer.registration_id
            invoice.buyer_snapshot_fiscal_code = invoice.buyer.fiscal_code
            invoice.buyer_snapshot_address = invoice.buyer.address
            invoice.buyer_snapshot_country = invoice.buyer.country
            invoice.buyer_snapshot_bank_account = invoice.buyer.bank_account
            invoice.buyer_snapshot_bank_name = invoice.buyer.bank_name
            changed = True
        if invoice.seller:
            invoice.seller_snapshot_name = invoice.seller.name
            invoice.seller_snapshot_owner_fullname = invoice.seller.owner_fullname
            invoice.seller_snapshot_registration_id = invoice.seller.registration_id
            invoice.seller_snapshot_fiscal_code = invoice.seller.fiscal_code
            invoice.seller_snapshot_address = invoice.seller.address
            invoice.seller_snapshot_country = invoice.seller.country
            invoice.seller_snapshot_bank_account = invoice.seller.bank_account
            invoice.seller_snapshot_bank_name = invoice.seller.bank_name
            changed = True
        if changed:
            invoice.save(
                update_fields=[
                    "buyer_snapshot_name",
                    "buyer_snapshot_owner_fullname",
                    "buyer_snapshot_registration_id",
                    "buyer_snapshot_fiscal_code",
                    "buyer_snapshot_address",
                    "buyer_snapshot_country",
                    "buyer_snapshot_bank_account",
                    "buyer_snapshot_bank_name",
                    "seller_snapshot_name",
                    "seller_snapshot_owner_fullname",
                    "seller_snapshot_registration_id",
                    "seller_snapshot_fiscal_code",
                    "seller_snapshot_address",
                    "seller_snapshot_country",
                    "seller_snapshot_bank_account",
                    "seller_snapshot_bank_name",
                ]
            )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0035_servicecontract_swap_buyer"),
    ]

    operations = [
        # Alter buyer FK to nullable SET_NULL
        migrations.AlterField(
            model_name="timeinvoice",
            name="buyer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="microinvoicer.fiscalentity",
            ),
        ),
        # Alter seller FK to nullable SET_NULL
        migrations.AlterField(
            model_name="timeinvoice",
            name="seller",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="microinvoicer.fiscalentity",
            ),
        ),
        # Add buyer snapshot fields
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_owner_fullname",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_registration_id",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_fiscal_code",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_address",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_country",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_bank_account",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="buyer_snapshot_bank_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        # Add seller snapshot fields
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_owner_fullname",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_registration_id",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_fiscal_code",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_address",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_country",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_bank_account",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="timeinvoice",
            name="seller_snapshot_bank_name",
            field=models.CharField(blank=True, max_length=255),
        ),
        # Populate snapshots from existing FK data
        migrations.RunPython(populate_invoice_snapshots, reverse_noop),
    ]
