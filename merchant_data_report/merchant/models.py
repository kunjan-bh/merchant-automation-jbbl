from django.db import models

class CBSMerchant(models.Model):
    merchant_id = models.CharField(max_length=100, unique=True, verbose_name="Merchant ID")
    merchant_code = models.CharField(max_length=100, verbose_name="Merchant Code")
    account_number = models.CharField(max_length=100, verbose_name="Account Number")
    province = models.CharField(max_length=100, verbose_name="Province")
    district = models.CharField(max_length=100, verbose_name="District")
    municipality = models.CharField(max_length=100, verbose_name="Municipality")
    address_1 = models.CharField(max_length=255, verbose_name="Address 1")
    address_2 = models.CharField(max_length=255, blank=True, null=True, verbose_name="Address 2")
    
    GENDER_CHOICES = [
        ('M', 'Male'),
        ('F', 'Female'),
        ('O', 'Other'),
    ]
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True, verbose_name="Gender")
    age = models.IntegerField(blank=True, null=True, verbose_name="Age")

    def __str__(self):
        return f"{self.merchant_code} - {self.merchant_id}"
