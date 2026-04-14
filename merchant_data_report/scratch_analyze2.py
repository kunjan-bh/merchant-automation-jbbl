import pandas as pd

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
phonepay_df = pd.read_excel(pay_path, nrows=50000) # Read enough rows

fp_mid_s2 = find_col_by_norm(f_step2, 'merchantid')
fp_acc_s2 = find_account_col(f_step2)

print("fp_mid_s2:", fp_mid_s2)
print("fp_acc_s2:", fp_acc_s2)

if fp_mid_s2 and fp_acc_s2:
    mid_to_acc = (
        f_step2[[fp_mid_s2, fp_acc_s2]]
        .dropna()
        .set_index(f_step2[fp_mid_s2].astype(str))[fp_acc_s2]
        .astype(str)
        .to_dict()
    )
    print("Size of mid_to_acc map:", len(mid_to_acc))
    
    mapped_count = 0
    total_count = len(phonepay_df)
    
    for _mid_col in ['MERCHANT_ID', 'MERCHANT_IDENTIFIER']:
        if _mid_col in phonepay_df.columns:
            print(f"Testing bridge with column {_mid_col}")
            mapped = phonepay_df[_mid_col].astype(str).map(mid_to_acc)
            mapped_count = mapped.notna().sum()
            print(f"Mapped {mapped_count} out of {total_count} ({mapped_count/total_count*100:.2f}%)")
            
            sample_ids = phonepay_df[_mid_col].astype(str).head(10).tolist()
            print("Sample IDs from Payment file:", sample_ids)
            print("Are they in the dictionary? ", [sid in mid_to_acc for sid in sample_ids])
            if sample_ids[0] in mid_to_acc:
                print("Mapped Account:", mid_to_acc[sample_ids[0]])
            break
