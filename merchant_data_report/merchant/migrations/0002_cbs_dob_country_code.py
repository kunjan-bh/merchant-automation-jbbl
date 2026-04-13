from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('merchant', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='cbsmerchant',
            name='age',
        ),
        migrations.AddField(
            model_name='cbsmerchant',
            name='dob',
            field=models.DateField(blank=True, null=True, verbose_name='Date of Birth'),
        ),
        migrations.AddField(
            model_name='cbsmerchant',
            name='country_code',
            field=models.CharField(blank=True, default='01', max_length=10, null=True, verbose_name='Country Code'),
        ),
        migrations.AlterField(
            model_name='cbsmerchant',
            name='account_number',
            field=models.CharField(db_index=True, max_length=100, verbose_name='Account Number'),
        ),
    ]
