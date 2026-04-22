import os
import django
import sys

# setup django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

from merchant.views import _generate_final_report
import openpyxl
import traceback

uid = 'f73b3fda'

try:
    _generate_final_report(uid)
    print("Report generated step passed")
    path = os.path.join('media', 'outputs', f'Additional_Payment_Report_ASCII_{uid}.xlsx')
    
    # Try reading it
    wb = openpyxl.load_workbook(path)
    print(f"Workbook loaded successfully. Sheets: {wb.sheetnames}")
    
except Exception as e:
    traceback.print_exc()
