import os
import glob
import pandas as pd

outputs_dir = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report\media\outputs"
print("Outputs dir:", outputs_dir)

for prefix in ['phonepay', 'cardless']:
    files = sorted(glob.glob(os.path.join(outputs_dir, f"{prefix}_*.xlsx")), key=os.path.getmtime, reverse=True)
    if files:
        print(f"Reading {files[0]}...")
        df = pd.read_excel(files[0])
        print(f"\n{prefix} columns:")
        for c in df.columns:
            print(f" - '{c}'")
