from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0032_alter_microregistry_include_vat"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicecontract",
            name="default_project",
            field=models.CharField(
                blank=True, default="", max_length=255, verbose_name="Default project name"
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="servicecontract",
            name="task_presets",
            field=models.TextField(
                blank=True,
                default="",
                verbose_name="Common task descriptions (one per line)",
            ),
            preserve_default=False,
        ),
        migrations.CreateModel(
            name="TimesheetEntry",
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
                ("work_date", models.DateField(verbose_name="Date")),
                ("project", models.CharField(blank=True, max_length=255)),
                ("activity", models.CharField(max_length=255, verbose_name="Task")),
                (
                    "hours",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=6,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0.01"))
                        ],
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timesheet_entries",
                        to="microinvoicer.timeinvoice",
                    ),
                ),
            ],
            options={
                "ordering": ["work_date", "id"],
            },
        ),
    ]
