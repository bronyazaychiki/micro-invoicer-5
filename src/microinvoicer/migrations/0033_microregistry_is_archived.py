from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0032_alter_microregistry_include_vat"),
    ]

    operations = [
        migrations.AddField(
            model_name="microregistry",
            name="is_archived",
            field=models.BooleanField(default=False),
        ),
    ]
