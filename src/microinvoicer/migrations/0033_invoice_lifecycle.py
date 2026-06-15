import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0032_alter_microregistry_include_vat"),
    ]

    operations = [
        migrations.AddField(
            model_name="timeinvoice",
            name="storno_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="storno_entries",
                to="microinvoicer.timeinvoice",
            ),
        ),
        migrations.AlterField(
            model_name="timeinvoice",
            name="number",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="timeinvoice",
            name="status",
            field=models.IntegerField(
                choices=[(0, "Draft"), (1, "Published"), (2, "Storno")], default=0
            ),
        ),
    ]
