from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('merchant', '0004_add_clean_cbs'),
    ]

    operations = [
        migrations.RenameField(
            model_name='cbsmerchant',
            old_name='address_2',
            new_name='address_3',
        ),
        migrations.RenameField(
            model_name='cleancbs',
            old_name='address_2',
            new_name='address_3',
        ),
    ]
