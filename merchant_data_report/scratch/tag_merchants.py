import os
import django

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

from merchant.models import CleanCBS

def cleanup():
    # Mark records that have geographical data (and were thus previously mapped) as merchants
    merchants = CleanCBS.objects.exclude(province='').exclude(district='').exclude(municipality='')
    count = merchants.update(is_merchant=True)
    print(f"Successfully tagged {count} existing records as Merchants.")

if __name__ == "__main__":
    cleanup()
