from django.db import migrations, models
import django.db.models.deletion
import django_countries.fields


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0032_alter_microregistry_include_vat"),
    ]

    operations = [
        migrations.CreateModel(
            name="Client",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("owner_fullname", models.CharField(max_length=255)),
                ("registration_id", models.CharField(max_length=40)),
                ("fiscal_code", models.CharField(max_length=40)),
                ("address", models.TextField()),
                (
                    "country",
                    django_countries.fields.CountryField(default="RO", max_length=2),
                ),
                ("bank_account", models.CharField(max_length=40)),
                ("bank_name", models.CharField(max_length=255)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "registry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="clients",
                        to="microinvoicer.microregistry",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.AddConstraint(
            model_name="client",
            constraint=models.UniqueConstraint(
                fields=["registry", "fiscal_code"],
                name="unique_client_fiscal_per_registry",
            ),
        ),
    ]
