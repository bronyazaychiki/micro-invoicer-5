# Generated for the registry archive feature

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('microinvoicer', '0032_alter_microregistry_include_vat'),
    ]

    operations = [
        migrations.AddField(
            model_name='microregistry',
            name='archived_at',
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
    ]
