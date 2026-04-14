import os
import django
import pandas as pd

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "merchant_data_report.settings")
django.setup()

from merchant.models import CleanCBS
from merchant.views import find_account_col, find_col_by_norm

uid = '356d3e8f'
f_path2 = os.path.join(r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs", f'step2_fonepay_{uid}.xlsx')
p_path = os.path.join(r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs", f'phonepay_{uid}.xlsx')

f_step2 = pd.read_excel(f_path2)
phonepay_df = pd.read_excel(p_path)

pp_acc_col = find_account_col(phonepay_df)
print(f"Initial pp_acc_col: {pp_acc_col}")

if pp_acc_col is None and not phonepay_df.empty and not f_step2.empty:
    fp_mid_s2 = find_col_by_norm(f_step2, 'merchantid')
    fp_acc_s2 = find_account_col(f_step2)
    print(f"fp_mid_s2: {fp_mid_s2}, fp_acc_s2: {fp_acc_s2}")
    if fp_mid_s2 and fp_acc_s2:
        mid_to_acc = (
            f_step2[[fp_mid_s2, fp_acc_s2]]
            .dropna()
            .set_index(f_step2[fp_mid_s2].astype(str))[fp_acc_s2]
            .astype(str)
            .to_dict()
        )
        print(f"mid_to_acc size: {len(mid_to_acc)}")
        for _mid_col in ['MERCHANT_ID', 'MERCHANT_IDENTIFIER']:
            if _mid_col in phonepay_df.columns:
                print(f"Found column {_mid_col} in phonepay_df")
                mapped = phonepay_df[_mid_col].astype(str).map(mid_to_acc)
                print(f"Mapped correctly: {mapped.notna().sum()}, NaNs: {mapped.isna().sum()}")
                phonepay_df['_account_number'] = mapped
                if phonepay_df['_account_number'].notna().any():
                    pp_acc_col = '_account_number'
                    break

print(f"Final pp_acc_col: {pp_acc_col}")

add_accs = set()
if pp_acc_col: add_accs.update(phonepay_df[pp_acc_col].dropna().astype(str).tolist())
print(f"add_accs size: {len(add_accs)}")

all_cbs_add = pd.DataFrame(list(CleanCBS.objects.filter(account_number__in=add_accs).values(
    'account_number', 'province', 'district', 'municipality', 'gender'
)))
print(f"CBS dataframe size: {len(all_cbs_add)}")

add_lookup = all_cbs_add.set_index('account_number') if not all_cbs_add.empty else pd.DataFrame()

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

print("Genders in patched phonepay_df:")
print(phonepay_df['gender'].value_counts(dropna=False))

