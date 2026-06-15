from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("microinvoicer", "0034_servicecontract_add_client"),
    ]

    operations = [
        # Remove the old buyer FK
        migrations.RemoveField(
            model_name="servicecontract",
            name="buyer",
        ),
        # Make client non-nullable (all rows now have a value)
        migrations.AlterField(
            model_name="servicecontract",
            name="client",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="contracts",
                to="microinvoicer.client",
            ),
        ),
    ]
