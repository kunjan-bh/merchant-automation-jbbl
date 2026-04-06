from django.shortcuts import render
from django.contrib import messages
from django.http import FileResponse, Http404
from django.conf import settings
import pandas as pd
import os
import uuid
import random
from .models import CBSMerchant

def upload_merchant_data(request):
    if request.method == 'POST':
        if 'file' not in request.FILES:
            messages.error(request, 'No file was uploaded.')
            return render(request, 'merchant/upload.html')
            
        file = request.FILES['file']
        
        if not file.name.endswith(('.xlsx', '.xls')):
            messages.error(request, 'Please upload a valid Excel file (.xlsx or .xls).')
            return render(request, 'merchant/upload.html')
            
        try:
            # Read the Fonepay sheet
            fonepay_df = pd.read_excel(file, sheet_name='fonepay')
            # Read the Nepal Pay sheet
            nepalpay_df = pd.read_excel(file, sheet_name='nepalpay')
            
            # Helper to filter columns based on a normalized match
            def filter_dataframe(df, requested_cols):
                cols_to_keep = []
                for col in df.columns:
                    norm_col = str(col).lower().replace(' ', '').replace('_', '')
                    if norm_col in requested_cols:
                        cols_to_keep.append(col)
                # If we didn't match any columns, just return the original dataframe to avoid empty file errors
                return df[cols_to_keep] if cols_to_keep else df

            fonepay_requested = ['merchantid', 'accountnumber', 'province', 'district', 'municipality', 'amount', 'tax', 'taxes', 'count']
            nepalpay_requested = ['province', 'district', 'accountnumber', 'merchantcode']
            
            # Filter the DataFrames
            fonepay_df = filter_dataframe(fonepay_df, fonepay_requested)
            nepalpay_df = filter_dataframe(nepalpay_df, nepalpay_requested)
            
            # --- START STEP 2 LOGIC ---
            def find_account_col(df):
                for col in df.columns:
                    if str(col).lower().replace(' ', '').replace('_', '') == 'accountnumber':
                        return col
                return None
            
            fonepay_acc_col = find_account_col(fonepay_df)
            nepalpay_acc_col = find_account_col(nepalpay_df)
            
            all_accounts = set()
            if fonepay_acc_col and fonepay_acc_col in fonepay_df.columns:
                all_accounts.update(fonepay_df[fonepay_acc_col].dropna().astype(str).tolist())
            if nepalpay_acc_col and nepalpay_acc_col in nepalpay_df.columns:
                all_accounts.update(nepalpay_df[nepalpay_acc_col].dropna().astype(str).tolist())
                
            existing_merchants = CBSMerchant.objects.filter(account_number__in=all_accounts)
            existing_accounts = set(existing_merchants.values_list('account_number', flat=True))
            missing_accounts = all_accounts - existing_accounts
            
            mock_merchants = []
            for acc in missing_accounts:
                mock_merchants.append(CBSMerchant(
                    merchant_id=f"M_{acc}_{random.randint(1000, 9999)}",
                    merchant_code=f"C_{random.randint(100, 999)}",
                    account_number=acc,
                    province=random.choice(["Province 1", "Madhesh", "Bagmati", "Gandaki", "Lumbini"]),
                    district=random.choice(["Kathmandu", "Lalitpur", "Bhaktapur", "Pokhara", "Chitwan"]),
                    municipality=random.choice(["KMC", "LMC", "PMC", "BHR", "BKT"]),
                    address_1=f"{random.randint(1, 100)} Random St",
                    address_2=random.choice([f"Ward {random.randint(1, 32)}", "Near ATM", ""]),
                    gender=random.choice(['M', 'F', 'O']),
                    age=random.randint(18, 70),
                ))
            
            if mock_merchants:
                CBSMerchant.objects.bulk_create(mock_merchants)
                
            all_cbs_data = pd.DataFrame(list(CBSMerchant.objects.filter(account_number__in=all_accounts).values(
                'account_number', 'address_1', 'address_2', 'gender', 'age', 'municipality'
            )))
            
            fonepay_step2_df = fonepay_df.copy()
            nepalpay_step2_df = nepalpay_df.copy()
            
            if not all_cbs_data.empty:
                if fonepay_acc_col:
                    fonepay_step2_df[fonepay_acc_col] = fonepay_step2_df[fonepay_acc_col].astype(str)
                    cbs_fonepay = all_cbs_data[['account_number', 'address_1', 'address_2', 'gender', 'age']].rename(columns={'account_number': fonepay_acc_col})
                    fonepay_step2_df = fonepay_step2_df.merge(cbs_fonepay, on=fonepay_acc_col, how='left')
                
                if nepalpay_acc_col:
                    nepalpay_step2_df[nepalpay_acc_col] = nepalpay_step2_df[nepalpay_acc_col].astype(str)
                    cbs_nepalpay = all_cbs_data[['account_number', 'address_1', 'address_2', 'gender', 'age', 'municipality']].copy()
                    cbs_nepalpay['municipality'] = None  # Force null per requirements
                    cbs_nepalpay = cbs_nepalpay.rename(columns={'account_number': nepalpay_acc_col})
                    nepalpay_step2_df = nepalpay_step2_df.merge(cbs_nepalpay, on=nepalpay_acc_col, how='left')
            # --- END STEP 2 LOGIC ---
            
            # Create a directory to store the separated files temporarily
            output_dir = os.path.join(settings.BASE_DIR, 'media', 'outputs')
            os.makedirs(output_dir, exist_ok=True)
            
            # Generate unique filenames to avoid collision
            unique_id = str(uuid.uuid4())[:8]
            fonepay_filename = f'step1_fonepay_{unique_id}.xlsx'
            nepalpay_filename = f'step1_nepalpay_{unique_id}.xlsx'
            fonepay_step2_filename = f'step2_fonepay_{unique_id}.xlsx'
            nepalpay_step2_filename = f'step2_nepalpay_{unique_id}.xlsx'
            
            fonepay_path = os.path.join(output_dir, fonepay_filename)
            nepalpay_path = os.path.join(output_dir, nepalpay_filename)
            fonepay_step2_path = os.path.join(output_dir, fonepay_step2_filename)
            nepalpay_step2_path = os.path.join(output_dir, nepalpay_step2_filename)
            
            # Save the separated dataframes back to new Excel files
            fonepay_df.to_excel(fonepay_path, index=False)
            nepalpay_df.to_excel(nepalpay_path, index=False)
            fonepay_step2_df.to_excel(fonepay_step2_path, index=False)
            nepalpay_step2_df.to_excel(nepalpay_step2_path, index=False)
            
            messages.success(request, 'Successfully processed and enriched the data sheets!')
            
            # Return template with context to show download buttons
            context = {
                'fonepay_count': len(fonepay_df),
                'nepalpay_count': len(nepalpay_df),
                'fonepay_filename': fonepay_filename,
                'nepalpay_filename': nepalpay_filename,
                'fonepay_step2_filename': fonepay_step2_filename,
                'nepalpay_step2_filename': nepalpay_step2_filename,
            }
            return render(request, 'merchant/upload.html', context)
            
        except ValueError as e:
            messages.error(request, f'Error reading sheets (Ensure "fonepay" and "nepalpay" exist in the document): {str(e)}')
        except Exception as e:
            messages.error(request, f'An unexpected error occurred: {str(e)}')
            
    return render(request, 'merchant/upload.html')

def download_sheet(request, filename):
    file_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', filename)
    if os.path.exists(file_path):
        return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=filename)
    else:
        raise Http404("File not found")

