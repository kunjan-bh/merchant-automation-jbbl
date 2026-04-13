from django.db import models

class CBSMerchant(models.Model):
    account_number = models.CharField(max_length=100, verbose_name="Account Number", db_index=True)
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
    dob = models.DateField(blank=True, null=True, verbose_name="Date of Birth")
    country_code = models.CharField(max_length=10, blank=True, null=True, default='01', verbose_name="Country Code")

    def __str__(self):
        return self.account_number


class CleanCBS(models.Model):
    """
    Platform-owned clean copy of CBS data.
    Initialised from CBSMerchant (read-only bank data) and enriched
    by manual user fills. The bank's CBSMerchant is never written to.
    All report generation reads from this table.
    """
    account_number = models.CharField(max_length=100, unique=True, db_index=True, verbose_name="Account Number")
    province = models.CharField(max_length=100, blank=True, null=True, verbose_name="Province")
    district = models.CharField(max_length=100, blank=True, null=True, verbose_name="District")
    municipality = models.CharField(max_length=100, blank=True, null=True, verbose_name="Municipality")
    address_1 = models.CharField(max_length=255, blank=True, null=True, verbose_name="Address 1")
    address_2 = models.CharField(max_length=255, blank=True, null=True, verbose_name="Address 2")

    GENDER_CHOICES = [
        ('M', 'Male'),
        ('F', 'Female'),
        ('O', 'Other'),
        ('C', 'Company'),
    ]
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True, verbose_name="Gender")
    dob = models.DateField(blank=True, null=True, verbose_name="Date of Birth")
    country_code = models.CharField(max_length=10, blank=True, null=True, default='01', verbose_name="Country Code")

    class Meta:
        verbose_name = "Clean CBS Record"
        verbose_name_plural = "Clean CBS Records"

    def __str__(self):
        return self.account_number
