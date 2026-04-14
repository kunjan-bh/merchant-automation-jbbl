import os
import django
import pandas as pd
import sys

# Setup Django environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "merchant_data_report.settings")
django.setup()

from merchant.models import CleanCBS

def find_account_col(df):
    for col in df.columns:
        norm = str(col).lower().replace(' ', '').replace('_', '')
        if norm in ['accountnumber', 'merchantaccount']:
            return col
    return None

def find_col_by_norm(df, norm_name):
    for col in df.columns:
        if str(col).lower().replace(' ', '').replace('_', '') == norm_name:
            return col
    return None

fp_st1_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\step2_fonepay_1cff9a01.xlsx"
pay_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\phonepay_1cff9a01.xlsx"

f_step2 = pd.read_excel(fp_st1_path)
phonepay_df = pd.read_excel(pay_path, nrows=1000) 

fp_mid_s2 = find_col_by_norm(f_step2, 'merchantid')
fp_acc_s2 = find_account_col(f_step2)

mid_to_acc = (
    f_step2[[fp_mid_s2, fp_acc_s2]]
    .dropna()
    .set_index(f_step2[fp_mid_s2].astype(str))[fp_acc_s2]
    .astype(str)
    .to_dict()
)

mapped = phonepay_df['MERCHANT_ID'].astype(str).map(mid_to_acc)
add_accs = set(mapped.dropna().tolist())

print(f"Total unique account numbers mapped: {len(add_accs)}")

# Check what CleanCBS has for these accounts
cbs_rows = list(CleanCBS.objects.filter(account_number__in=add_accs).values(
    'account_number', 'gender', 'province'
))

print(f"Found {len(cbs_rows)} out of {len(add_accs)} accounts in CleanCBS.")

df_cbs = pd.DataFrame(cbs_rows)
if not df_cbs.empty:
    print("\nGender value counts in CleanCBS for these accounts:")
    print(df_cbs['gender'].value_counts(dropna=False))
else:
    print("CleanCBS returned no data for these accounts!")
