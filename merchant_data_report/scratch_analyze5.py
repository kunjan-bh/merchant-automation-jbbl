import os
import django
import pandas as pd
import re

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

def get_col_txns(df, possible_names):
    if df is None or df.empty: return None
    def clean(s): return re.sub(r'[^a-z0-9]', '', str(s).lower())
    for pn in possible_names:
        pn_clean = clean(pn)
        for c in df.columns:
            if pn_clean == clean(c): return c
    for pn in possible_names:
        pn_clean = clean(pn)
        for c in df.columns:
            if clean(c).startswith(pn_clean): return c
    for pn in possible_names:
        pn_clean = clean(pn)
        for c in df.columns:
            if pn_clean in clean(c): return c
    return None

def get_norm_df_with_amount(df, acc_col, amt_cols):
    if df is None or df.empty: return pd.DataFrame()
    temp = pd.DataFrame()
    temp['account_number'] = df[acc_col] if acc_col and acc_col in df.columns else None
    for p in ['province', 'district', 'municipality', 'gender']:
        first_col = last_col = None
        for c in df.columns:
            if str(c).lower().replace(' ', '').replace('_', '') == p:
                if first_col is None:
                    first_col = c
                last_col = c
        if first_col is None:
            temp[p] = None
        elif first_col == last_col:
            temp[p] = df[first_col]
        else:
            orig = df[first_col]
            cbs  = df[last_col]
            empty_mask = orig.isna() | (orig.astype(str).str.strip() == '') | (orig.astype(str).str.lower() == 'null')
            temp[p] = orig.where(~empty_mask, cbs)

    amt_col = get_col_txns(df, amt_cols)
    temp['amount'] = pd.to_numeric(df[amt_col], errors='coerce').fillna(0) if amt_col else 0.0
    return temp

def map_gender(g):
    if pd.isna(g) or str(g).strip() == '': return 'Company'
    g_str = str(g).strip().upper()
    if g_str in ['M', 'MALE']: return 'Male'
    if g_str in ['F', 'FEMALE']: return 'Female'
    return 'Company'

fp_st1_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\step2_fonepay_1cff9a01.xlsx"
pay_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\phonepay_1cff9a01.xlsx"

f_step2 = pd.read_excel(fp_st1_path)
phonepay_df = pd.read_excel(pay_path, nrows=5000)

pp_acc_col = find_account_col(phonepay_df)
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

print("Final genders in pp_norm:")
print(pp_norm['gender'].value_counts(dropna=False))

print("Final mapped genders in pp_norm:")
print(pp_norm['gender'].apply(map_gender).value_counts(dropna=False))
