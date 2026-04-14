import os
import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "merchant_data_report.settings")
django.setup()

from merchant.models import CleanCBS
from merchant.views import get_norm_df_with_amount, get_col_txns

fp_st1_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\step2_fonepay_1cff9a01.xlsx"
pay_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\phonepay_1cff9a01.xlsx"

f_step2 = pd.read_excel(fp_st1_path)
phonepay_df = pd.read_excel(pay_path, nrows=50000)

for _mid_col in ['MERCHANT_ID', 'MERCHANT_IDENTIFIER']:
    if _mid_col in phonepay_df.columns:
        fp_mid_s2 = [c for c in f_step2.columns if str(c).lower().replace(' ', '').replace('_', '') == 'merchantid'][0]
        fp_acc_s2 = [c for c in f_step2.columns if str(c).lower().replace(' ', '').replace('_', '') in ['accountnumber', 'merchantaccount']][0]
        
        mid_to_acc = f_step2[[fp_mid_s2, fp_acc_s2]].dropna().set_index(f_step2[fp_mid_s2].astype(str))[fp_acc_s2].astype(str).to_dict()
        phonepay_df['_account_number'] = phonepay_df[_mid_col].astype(str).map(mid_to_acc)
        break

pp_acc_col = '_account_number'
add_accs = set(phonepay_df[pp_acc_col].dropna().astype(str).tolist())

all_cbs_add = pd.DataFrame(list(CleanCBS.objects.filter(account_number__in=add_accs).values(
    'account_number', 'province', 'district', 'municipality', 'gender'
)))
add_lookup = all_cbs_add.set_index('account_number')

def patch_df_vectorized(df, acc_col, exclude_cols=None):
    if not acc_col or add_lookup.empty: return
    if exclude_cols is None: exclude_cols = set()
    accs = df[acc_col].astype(str)
    for col in ['province', 'district', 'municipality', 'gender']:
        if col in exclude_cols: continue
        if col not in df.columns: df[col] = None
        is_empty_mask = df[col].isna() | (df[col].astype(str).str.strip() == '') | (df[col].astype(str).str.lower() == 'null')
        if col in add_lookup.columns:
            map_s = accs.map(add_lookup[col])
            valid_map = map_s.notna() & (map_s.astype(str).str.strip() != '') & (map_s.astype(str).str.lower() != 'null')
            df.loc[is_empty_mask & valid_map, col] = map_s[is_empty_mask & valid_map]

patch_df_vectorized(phonepay_df, pp_acc_col)

pp_norm = get_norm_df_with_amount(phonepay_df, pp_acc_col, ['originalamount', 'amount'])
print(f"Norm df length: {len(pp_norm)}")
print("Gender counts from get_norm_df_with_amount:")
print(pp_norm['gender'].value_counts(dropna=False))

def map_gender(g):
    if pd.isna(g) or str(g).strip() == '': return 'Company'
    g_str = str(g).strip().upper()
    if g_str in ['M', 'MALE']: return 'Male'
    if g_str in ['F', 'FEMALE']: return 'Female'
    return 'Company'

print("\nAfter map_gender:")
print(pp_norm['gender'].apply(map_gender).value_counts(dropna=False))
