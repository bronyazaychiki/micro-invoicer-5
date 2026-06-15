# Generated manually for real timesheet feature

import django.core.validators
from decimal import Decimal
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('microinvoicer', '0032_alter_microregistry_include_vat'),
    ]

    operations = [
        migrations.CreateModel(
            name='TimesheetEntry',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(verbose_name='Entry date')),
                ('project', models.CharField(blank=True, max_length=255)),
                ('task', models.CharField(max_length=255, verbose_name='Activity / task description')),
                ('hours', models.DecimalField(decimal_places=2, max_digits=8, validators=[django.core.validators.MinValueValidator(Decimal('0.25'))], verbose_name='Hours')),
                ('invoice', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='timesheet_entries', to='microinvoicer.timeinvoice')),
            ],
            options={
                'ordering': ['date', 'id'],
            },
        ),
        migrations.CreateModel(
            name='TimesheetTemplate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('project', models.CharField(blank=True, max_length=255, verbose_name='Project name')),
                ('task', models.CharField(max_length=255, verbose_name='Default task description')),
                ('contract', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='timesheet_templates', to='microinvoicer.servicecontract')),
            ],
            options={
                'ordering': ['project', 'task'],
            },
        ),
    ]
