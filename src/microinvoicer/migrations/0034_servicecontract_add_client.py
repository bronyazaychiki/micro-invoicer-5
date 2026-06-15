from django.db import migrations, models
import django.db.models.deletion


def create_clients_from_contracts(apps, schema_editor):
    """Create Client records from existing buyer FiscalEntity data."""
    ServiceContract = apps.get_model("microinvoicer", "ServiceContract")
    Client = apps.get_model("microinvoicer", "Client")

    for contract in ServiceContract.objects.select_related("buyer", "registry").all():
        buyer = contract.buyer
        # Deduplicate within registry: reuse Client if same fiscal_code exists
        client, created = Client.objects.get_or_create(
            registry=contract.registry,
            fiscal_code=buyer.fiscal_code,
            defaults={
                "name": buyer.name,
                "owner_fullname": buyer.owner_fullname,
                "registration_id": buyer.registration_id,
                "address": buyer.address,
                "country": buyer.country,
                "bank_account": buyer.bank_account,
                "bank_name": buyer.bank_name,
            },
        )
        contract.client = client
        contract.save(update_fields=["client_id"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0033_client"),
    ]

    operations = [
        # Step 1: Add nullable client FK
        migrations.AddField(
            model_name="servicecontract",
            name="client",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="contracts",
                to="microinvoicer.client",
            ),
        ),
        # Step 2: Populate from existing buyer data
        migrations.RunPython(create_clients_from_contracts, reverse_noop),
    ]
