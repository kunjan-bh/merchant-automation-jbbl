import pandas as pd

fp_st1_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\step2_fonepay_1cff9a01.xlsx"
pay_path = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs\phonepay_1cff9a01.xlsx"

f_step2 = pd.read_excel(fp_st1_path)
print("Fonepay Step 2 Columns:", f_step2.columns.tolist())

# Using usecols if possible, but let's just read first 50 rows
pay_df = pd.read_excel(pay_path, nrows=100)
print("Payment Details Columns:", pay_df.columns.tolist())

mid_col_f = [c for c in f_step2.columns if 'merchant' in c.lower() and 'id' in c.lower()]
mid_col_pay = [c for c in pay_df.columns if 'merchant' in c.lower() and 'id' in c.lower() or 'identifier' in c.lower()]

print("\n--- Column Matching ---")
print("Found Merchant ID cols in Step 2:", mid_col_f)
print("Found Merchant ID cols in Payment details:", mid_col_pay)

if mid_col_f:
    print("\nSample values from Step 2:", f_step2[mid_col_f[0]].head(5).tolist())
    print("Types in Step 2:", f_step2[mid_col_f[0]].apply(type).value_counts().to_dict())
    
if mid_col_pay:
    print("\nSample values from Payment:", pay_df[mid_col_pay[0]].head(5).tolist())
    print("Types in Payment:", pay_df[mid_col_pay[0]].apply(type).value_counts().to_dict())
