from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0032_alter_microregistry_include_vat"),
    ]

    operations = [
        migrations.AlterField(
            model_name="timeinvoice",
            name="number",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
