import os
import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "merchant_data_report.settings")
django.setup()

from merchant.models import CleanCBS
from merchant.views import map_gender

fp_st1_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\step2_fonepay_1cff9a01.xlsx"
pay_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\phonepay_1cff9a01.xlsx"

f_step2 = pd.read_excel(fp_st1_path)
phonepay_df = pd.read_excel(pay_path)

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

fp_mid_s2 = find_col_by_norm(f_step2, 'merchantid')
fp_acc_s2 = find_account_col(f_step2)
mid_to_acc = f_step2[[fp_mid_s2, fp_acc_s2]].dropna().set_index(f_step2[fp_mid_s2].astype(str))[fp_acc_s2].astype(str).to_dict()

for _mid_col in ['MERCHANT_ID', 'MERCHANT_IDENTIFIER']:
    if _mid_col in phonepay_df.columns:
        phonepay_df['_account_number'] = phonepay_df[_mid_col].astype(str).map(mid_to_acc)
        pp_acc_col = '_account_number'
        break

add_accs = set(phonepay_df[pp_acc_col].dropna().astype(str).tolist())
all_cbs_add = pd.DataFrame(list(CleanCBS.objects.filter(account_number__in=add_accs).values(
    'account_number', 'gender'
)))
add_lookup = all_cbs_add.set_index('account_number')

raw_genders = phonepay_df[pp_acc_col].astype(str).map(add_lookup['gender'] if not add_lookup.empty else {})
print("Raw Gender values right out of CBS map:")
print(raw_genders.value_counts(dropna=False))

mapped_genders = raw_genders.apply(map_gender)
print("\nAfter map_gender() logic:")
print(mapped_genders.value_counts(dropna=False))
