import os
import sys
import django
sys.path.append(r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

import glob
from merchant.views import _generate_final_report

latest_step2 = glob.glob(r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\step2_fonepay_*.xlsx")
if not latest_step2:
    print("NO FILES FOUND")
else:
    uid = latest_step2[-1].split('_')[-1].split('.')[0]
    print(f"USING UID: {uid}")
    try:
        _generate_final_report(uid)
        print("REPORT GENERATED WITH LATEST FIX!")
    except Exception as e:
        print(f"FAILED: {e}")
