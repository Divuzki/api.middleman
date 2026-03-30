from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agreement', '0009_agreement_agreement_type_agreement_fee_payer'),
    ]

    operations = [
        migrations.AddField(
            model_name='agreement',
            name='pending_seller_fee',
            field=models.DecimalField(
                max_digits=12,
                decimal_places=2,
                null=True,
                blank=True,
                help_text='Seller escrow fee locked in at accept_offer time. Used by confirm_agreement.',
            ),
        ),
    ]
