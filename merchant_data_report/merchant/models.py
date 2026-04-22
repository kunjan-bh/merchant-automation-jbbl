from django.db import models
import os


class ReportBatch(models.Model):
    """One row per monthly NRB merchant-data report generation.

    Becomes visible to all officers (shared panel) only when
    status='completed', has_errors=False, verified=True.
    """

    STATUS_CHOICES = [
        ('pending',    'Pending'),
        ('processing', 'Processing'),
        ('completed',  'Completed'),
        ('error',      'Error'),
    ]

    # Who generated
    generated_by      = models.CharField(max_length=100, db_index=True)
    generated_by_name = models.CharField(max_length=200, blank=True)

    # Report period (Nepali BS)
    month         = models.CharField(max_length=20)
    year          = models.PositiveIntegerField()
    report_period = models.CharField(max_length=50, blank=True)

    institution_name = models.CharField(max_length=200, default='Jyoti Bikas Bank Limited')
    institution_code = models.CharField(max_length=20,  default='12060')

    # Pipeline handle (links DB row to the file-based pipeline outputs)
    unique_id = models.CharField(max_length=32, blank=True, db_index=True)

    # Uploaded input filenames (for display only — actual files are at media/outputs/<prefix>_<uid>.xlsx)
    upload_filename           = models.CharField(max_length=255, blank=True)
    card_data_filename        = models.CharField(max_length=255, blank=True)
    phonepay_filename         = models.CharField(max_length=255, blank=True)
    nepalpay_filename         = models.CharField(max_length=255, blank=True)
    cardless_filename         = models.CharField(max_length=255, blank=True)
    mobile_banking_filename   = models.CharField(max_length=255, blank=True)
    connect_ips_filename      = models.CharField(max_length=255, blank=True)

    # Final report filename
    final_report_filename = models.CharField(max_length=255, blank=True)

    # Stats
    total_records         = models.IntegerField(default=0)
    fonepay_count         = models.IntegerField(default=0)
    nepalpay_count        = models.IntegerField(default=0)
    invalid_account_count = models.IntegerField(default=0)
    merged_cbs_count      = models.IntegerField(default=0)

    # Status
    status        = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True)
    has_errors    = models.BooleanField(default=False)
    verified      = models.BooleanField(default=False)  # officer confirms error-free → shared

    # Timestamps
    created_at   = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name        = 'Report Batch'
        verbose_name_plural = 'Report Batches'

    def save(self, *args, **kwargs):
        if self.month and self.year:
            self.report_period = f'{self.month} {self.year}'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.report_period} — {self.generated_by} ({self.status})'


class UserActivityLog(models.Model):
    """Every significant user action. Logged to DB + logs/activity.log in parallel."""

    LEVEL_CHOICES = [
        ('INFO',    'Info'),
        ('WARNING', 'Warning'),
        ('ERROR',   'Error'),
    ]

    ACTION_CHOICES = [
        ('LOGIN',            'Login'),
        ('LOGIN_FAIL',       'Login Failed'),
        ('LOGOUT',           'Logout'),
        ('GENERATE_START',   'Generate Started'),
        ('GENERATE_SUCCESS', 'Generate Success'),
        ('GENERATE_ERROR',   'Generate Error'),
        ('DOWNLOAD',         'Download'),
        ('VERIFY',           'Verified Report'),
        ('UNVERIFY',         'Unverified Report'),
    ]

    timestamp  = models.DateTimeField(auto_now_add=True, db_index=True)
    username   = models.CharField(max_length=100, db_index=True)
    full_name  = models.CharField(max_length=200, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    level      = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='INFO', db_index=True)
    action     = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True)
    detail     = models.TextField(blank=True)
    batch      = models.ForeignKey(
        'ReportBatch', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='activity_logs',
    )

    class Meta:
        ordering = ['-timestamp']
        verbose_name        = 'User Activity Log'
        verbose_name_plural = 'User Activity Logs'

    def __str__(self):
        return f'[{self.timestamp:%Y-%m-%d %H:%M:%S}] {self.username} — {self.action}'


class CBSMerchant(models.Model):
    account_number = models.CharField(max_length=100, verbose_name="Account Number", db_index=True)
    province = models.CharField(max_length=100, verbose_name="Province")
    district = models.CharField(max_length=100, verbose_name="District")
    municipality = models.CharField(max_length=100, verbose_name="Municipality")
    address_1 = models.CharField(max_length=255, verbose_name="Address 1")
    address_3 = models.CharField(max_length=255, blank=True, null=True, verbose_name="Address 3")

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
    address_3 = models.CharField(max_length=255, blank=True, null=True, verbose_name="Address 3")

    GENDER_CHOICES = [
        ('M', 'Male'),
        ('F', 'Female'),
        ('O', 'Other'),
        ('C', 'Company'),
    ]
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True, verbose_name="Gender")
    dob = models.DateField(blank=True, null=True, verbose_name="Date of Birth")
    country_code = models.CharField(max_length=10, blank=True, null=True, default='01', verbose_name="Country Code")
    # None = neutral (from CBS, unknown), True = confirmed merchant (in input file), False = confirmed non-merchant
    is_merchant = models.BooleanField(default=None, null=True, blank=True, verbose_name="Is Merchant")

    class Meta:
        verbose_name = "Clean CBS Record"
        verbose_name_plural = "Clean CBS Records"

    def __str__(self):
        return self.account_number
