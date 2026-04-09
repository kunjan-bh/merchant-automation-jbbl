import pandas as pd
import os
import glob

# Find latest outputs
outputs_dir = r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\media\outputs"
phonepay_files = sorted(glob.glob(os.path.join(outputs_dir, "phonepay_*.xlsx")), key=os.path.getmtime, reverse=True)
cardless_files = sorted(glob.glob(os.path.join(outputs_dir, "cardless_*.xlsx")), key=os.path.getmtime, reverse=True)

if phonepay_files:
    df = pd.read_excel(phonepay_files[0])
    print("PhonePay columns:")
    for c in df.columns:
        print(f" - '{c}'")
        if 'issuer' in str(c).lower():
            print("   -> Issuer values sample:", df[c].dropna().unique()[:5])

if cardless_files:
    df = pd.read_excel(cardless_files[0])
    print("Cardless columns:")
    for c in df.columns:
        print(f" - '{c}'")
