import os, sys, django
sys.path.append(r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

from merchant.views import _generate_final_report
import pandas as pd

uid = "c1235d0c"
_generate_final_report(uid)

f = fr'media\outputs\Additional_Payment_Report_ASCII_{uid}.xlsx'
df = pd.read_excel(f, sheet_name='7. Merchant Txns_Province', header=1)
print('Total QR count in Sheet 7:', df.loc[df.iloc[:, 4] == 'Total'].iloc[0, 5])
