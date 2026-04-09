import os
import django
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

from django.conf import settings
import glob
import pandas as pd

outputs_dir = os.path.join(settings.BASE_DIR, 'media', 'outputs')
print("Checking outputs dir:", outputs_dir)

for prefix in ['phonepay', 'cardless']:
    files = sorted(glob.glob(os.path.join(outputs_dir, f"{prefix}_*.xlsx")), key=os.path.getmtime, reverse=True)
    if files:
        df = pd.read_excel(files[0])
        print(f"\n{prefix} columns:")
        for c in df.columns:
            print(f" - '{c}'")
        if prefix == 'phonepay':
            for c in df.columns:
                if 'issuer' in str(c).lower():
                    print("Issuer values sample:", df[c].dropna().unique()[:5])
    else:
        print(f"No {prefix} files found!")
