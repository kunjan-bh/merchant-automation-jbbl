import os
import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "merchant_data_report.settings")
django.setup()

from merchant.models import CleanCBS
from merchant.views import map_gender

np_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\nepalpay_1cff9a01.xlsx"
nepalpay_df = pd.read_excel(np_path)

def find_account_col(df):
    for col in df.columns:
        norm = str(col).lower().replace(' ', '').replace('_', '')
        if norm in ['accountnumber', 'merchantaccount']:
            return col
    return None

np2_acc_col = find_account_col(nepalpay_df)

if np2_acc_col:
    add_accs = set(nepalpay_df[np2_acc_col].dropna().astype(str).tolist())
    all_cbs_add = pd.DataFrame(list(CleanCBS.objects.filter(account_number__in=add_accs).values(
        'account_number', 'gender'
    )))
    add_lookup = all_cbs_add.set_index('account_number')
    
    raw_genders = nepalpay_df[np2_acc_col].astype(str).map(add_lookup['gender'] if not add_lookup.empty else {})
    print(f"Total Nepalpay Rows: {len(nepalpay_df)}")
    print("\nRaw NepalPay CBS Genders:")
    print(raw_genders.value_counts(dropna=False))
else:
    print("Could not find account column in NepalPay.")
