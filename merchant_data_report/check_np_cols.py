import pandas as pd
import os
import glob
outputs_dir = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\media\outputs"
nepalpay_files = sorted(glob.glob(os.path.join(outputs_dir, "nepalpay_*.xlsx")), key=os.path.getmtime, reverse=True)

if nepalpay_files:
    df = pd.read_excel(nepalpay_files[0])
    print("NepalPay columns:")
    for c in df.columns:
        print(f" - '{c}'")
    print("\nFirst few rows:")
    print(df.head(2))
