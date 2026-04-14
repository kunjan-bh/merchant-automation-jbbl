import os
import pandas as pd

fp_st1_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\step2_fonepay_1cff9a01.xlsx"
pay_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\phonepay_1cff9a01.xlsx"

print("Loading step 2...")
f_step2 = pd.read_excel(fp_st1_path)

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

print("Loading payment details...")
phonepay_df = pd.read_excel(pay_path)

fp_mid_s2 = find_col_by_norm(f_step2, 'merchantid')
fp_acc_s2 = find_account_col(f_step2)

mid_to_acc = f_step2[[fp_mid_s2, fp_acc_s2]].dropna().set_index(f_step2[fp_mid_s2].astype(str))[fp_acc_s2].astype(str).to_dict()

total_rows = len(phonepay_df)
print(f"Total rows in phonepay_df: {total_rows}")

for _mid_col in ['MERCHANT_ID', 'MERCHANT_IDENTIFIER']:
    if _mid_col in phonepay_df.columns:
        mapped = phonepay_df[_mid_col].astype(str).map(mid_to_acc)
        successfully_mapped = mapped.notna().sum()
        failed_to_map = mapped.isna().sum()
        
        print(f"Using column: {_mid_col}")
        print(f"Successfully mapped: {successfully_mapped} ({successfully_mapped/total_rows*100:.2f}%)")
        print(f"Failed to map: {failed_to_map} ({failed_to_map/total_rows*100:.2f}%)")
        break
