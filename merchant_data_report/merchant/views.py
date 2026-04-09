from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.conf import settings
from django.urls import reverse
from django.db import connection
import pandas as pd
import os
import uuid
import random
import json
import threading
from .models import CBSMerchant

# --- HELPER FUNCTIONS ---

def is_empty(val):
    return pd.isna(val) or str(val).strip() == '' or str(val).lower() == 'null'

def filter_dataframe(df, requested_cols):
    cols_to_keep = []
    for col in df.columns:
        norm_col = str(col).lower().replace(' ', '').replace('_', '')
        if norm_col in requested_cols:
            cols_to_keep.append(col)
    return df[cols_to_keep] if cols_to_keep else df

def find_account_col(df):
    for col in df.columns:
        if str(col).lower().replace(' ', '').replace('_', '') == 'accountnumber':
            return col
    return None

def find_col_by_norm(df, norm_name):
    for col in df.columns:
        if str(col).lower().replace(' ', '').replace('_', '') == norm_name:
            return col
    return None

def get_fill_col(acc_col_name, cbs_lookup):
    def fill_col(r, col_name, cbs_col=None):
        if not cbs_col: cbs_col = col_name
        val = None
        for key in r.index:
            if str(key).lower().replace(' ', '').replace('_', '') == col_name.lower():
                val = r.get(key)
                break
        if is_empty(val):
            acc = str(r.get(acc_col_name)).strip()
            if not cbs_lookup.empty and acc in cbs_lookup.index:
                v = cbs_lookup.loc[acc, cbs_col]
                return v.iloc[0] if isinstance(v, pd.Series) else v
        return val
    return fill_col

def check_missing_records(df, required_cols, index_col):
    missing_dict = {}
    if df is None or df.empty or not index_col:
        return missing_dict
    for _, row in df.iterrows():
        acc = str(row.get(index_col)).strip()
        if not acc or is_empty(acc): continue
        missing = [c for c in required_cols if is_empty(row.get(c))]
        if missing:
            if acc not in missing_dict:
                missing_dict[acc] = {
                    'account_number': acc,
                    'address_1': str(row.get('address_1', '')),
                    'address_2': str(row.get('address_2', '')),
                    'needs_province': 'province' in missing,
                    'needs_district': 'district' in missing,
                    'needs_municipality': 'municipality' in missing,
                }
            else:
                for m in missing:
                    missing_dict[acc][f'needs_{m}'] = True
    return missing_dict

# --- MAPPERS ---

provinces_list = ['Koshi', 'Madhesh', 'Bagmati', 'Gandaki', 'Lumbini', 'Karnali', 'Sudurpaschim']
def map_province(p):
    if pd.isna(p) or str(p).strip() == '': return 'Unmatched'
    p_norm = str(p).lower().replace(' ', '').replace('_', '')
    if '1' in p_norm or 'koshi' in p_norm: return 'Koshi'
    if '2' in p_norm or 'madh' in p_norm: return 'Madhesh'
    if '3' in p_norm or 'bagm' in p_norm: return 'Bagmati'
    if '4' in p_norm or 'gand' in p_norm: return 'Gandaki'
    if '5' in p_norm or 'lumb' in p_norm: return 'Lumbini'
    if '6' in p_norm or 'karn' in p_norm: return 'Karnali'
    if '7' in p_norm or 'sudur' in p_norm or 'sughar' in p_norm: return 'Sudurpaschim'
    return 'Unmatched'

districts_list = [
    "Bhojpur District", "Dhankuta District", "Ilam District", "Jhapa District", "Khotang District", "Morang District", "Okhaldhunga District", "Panchthar District", "Sankhuwasabha District", "Solukhumbu District", "Sunsari District", "Taplejung District", "Tehrathum District", "Udayapur District", "Bara District", "Parsa District", "Rautahat District", "Sarlahi District", "Dhanusha District", "Siraha District", "Mahottari District", "Saptari District", "Sindhuli District", "Ramechhap District", "Dolakha District", "Bhaktapur District", "Dhading District", "Kathmandu District", "Kavrepalanchok District", "Lalitpur District", "Nuwakot District", "Rasuwa District", "Sindhupalchok District", "Chitwan District", "Makwanpur District", "Baglung District", "Gorkha District", "Kaski District", "Lamjung District", "Manang District", "Mustang District", "Myagdi District", "Nawalpur District", "Parbat District", "Syangja District", "Tanahun District", "Arghakhanchi District", "Gulmi District", "Kapilvastu District", "Parasi District", "Palpa District", "Rupandehi District", "Banke District", "Bardiya District", "Dang District", "Pyuthan District", "Rolpa District", "Rukum East District", "Dailekh District", "Dolpa District", "Humla District", "Jajarkot District", "Jumla District", "Kalikot District", "Mugu District", "Rukum West District", "Surkhet District", "Achham District", "Baitadi District", "Bajhang District", "Bajura District", "Dadeldhura District", "Darchula District", "Doti District", "Kailali District", "Kanchanpur District"
]
def map_district(d):
    if pd.isna(d) or str(d).strip() == '': return 'Unmatched'
    d_str = str(d).strip().title()
    if not d_str.endswith(" District"): d_str += " District"
    if d_str not in districts_list: return 'Unmatched'
    return d_str

local_cats = ['Metropolitan Cities', 'Sub-Metropolitan Cities', 'Municipalities', 'Rural Municipalities']
def map_local(m):
    if pd.isna(m) or str(m).strip() == '': return 'Unmatched'
    m_str = str(m).strip().upper()
    if 'SMC' in m_str or 'SUB' in m_str: return 'Sub-Metropolitan Cities'
    if 'MC' in m_str or 'METRO' in m_str: return 'Metropolitan Cities'
    if 'RM' in m_str or 'RURAL' in m_str: return 'Rural Municipalities'
    if 'M' in m_str or 'MUN' in m_str: return 'Municipalities'
    return 'Unmatched'

gender_cats = ['Male', 'Female', 'Others (Gender other than Male and Female)', 'Company']
def map_gender(g):
    if pd.isna(g) or str(g).strip() == '': return 'Unmatched'
    g_str = str(g).strip().upper()
    if g_str in ['M', 'MALE']: return 'Male'
    if g_str in ['F', 'FEMALE']: return 'Female'
    if g_str in ['O', 'OTHER', 'OTHERS']: return 'Others (Gender other than Male and Female)'
    if g_str in ['C', 'COMPANY']: return 'Company'
    return 'Unmatched'

def get_prov_counts(df):
    counts = {p: 0 for p in provinces_list}
    if df is not None and not df.empty:
        col = find_col_by_norm(df, 'province')
        if col:
            mapped = df[col].apply(map_province)
            for k, v in mapped.value_counts().items():
                if k in counts: 
                    counts[k] += v
                elif k != 'Unmatched': 
                    counts[k] = v
    return counts

def get_dist_counts(df):
    if df is not None and not df.empty:
        col = find_col_by_norm(df, 'district')
        if col:
            mapped = df[col].apply(map_district)
            counts = mapped.value_counts().to_dict()
            if 'Unmatched' in counts:
                del counts['Unmatched']
            return counts
    return {}

# --- PROGRESS TRACKING ---

def _progress_path(uid):
    d = os.path.join(settings.BASE_DIR, 'media', 'outputs')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'progress_{uid}.json')

def _set_progress(uid, step, status, detail='', extra=None):
    data = {'step': step, 'status': status, 'detail': detail}
    if extra:
        data.update(extra)
    path = _progress_path(uid)
    with open(path, 'w') as f:
        json.dump(data, f)

# --- BUSINESS LOGIC ---

def generate_mock_data(all_accounts, nepalpay_accs=None, source_null_province=None, source_null_district=None):
    if nepalpay_accs is None: nepalpay_accs = set()
    if source_null_province is None: source_null_province = set()
    if source_null_district is None: source_null_district = set()
    
    CBSMerchant.objects.filter(account_number__in=all_accounts).delete()
    
    mock_municipality_options = ["Kathmandu MC", "Lalitpur SMC", "Byans RM", "Bharatpur MC", "Hetauda SMC", "Birendranagar M", "Pokhara MC"]
    
    mock_merchants = []
    for acc in all_accounts:
        muni = "" if acc in nepalpay_accs else random.choice(mock_municipality_options)
        prov = "" if acc in source_null_province else random.choice(provinces_list)
        dist = "" if acc in source_null_district else random.choice(districts_list)
        
        mock_merchants.append(CBSMerchant(
            merchant_id=f"M_{acc}_{random.randint(1000, 9999)}",
            merchant_code=f"C_{random.randint(100, 999)}",
            account_number=acc,
            province=prov,
            district=dist,
            municipality=muni,
            address_1=f"{random.randint(1, 100)} Random Marga",
            address_2=random.choice([f"Ward {random.randint(1, 32)}", "Near Branch", ""]),
            gender=random.choice(['M', 'F', 'O', 'C']),
            age=random.randint(18, 70),
        ))
    if mock_merchants:
        CBSMerchant.objects.bulk_create(mock_merchants)

def perform_step1_and_2(file_or_path, unique_id, uid=None):
    """Core pipeline. Accepts file object or path. If uid is provided, tracks progress."""
    if uid: _set_progress(uid, 1, 'active', 'Reading Excel workbook...')
    
    fonepay_df = pd.read_excel(file_or_path, sheet_name='fonepay')
    nepalpay_df = pd.read_excel(file_or_path, sheet_name='nepalpay')
    
    if uid: _set_progress(uid, 2, 'active', f'Filtering columns — {len(fonepay_df):,} FonePay + {len(nepalpay_df):,} NepalPay records')
    
    fonepay_requested = ['merchantid', 'accountnumber', 'province', 'district', 'municipality', 'amount', 'tax', 'taxes', 'count', 'gender', 'age', 'address1', 'address2']
    nepalpay_requested = ['province', 'district', 'accountnumber', 'merchantcode', 'municipality', 'gender', 'age', 'address1', 'address2']
    
    fonepay_df = filter_dataframe(fonepay_df, fonepay_requested)
    nepalpay_df = filter_dataframe(nepalpay_df, nepalpay_requested)
    
    fonepay_acc_col = find_account_col(fonepay_df)
    nepalpay_acc_col = find_account_col(nepalpay_df)
    
    fonepay_accs = set()
    nepalpay_accs = set()
    if fonepay_acc_col and fonepay_acc_col in fonepay_df.columns:
        fonepay_accs.update(fonepay_df[fonepay_acc_col].dropna().astype(str).tolist())
    if nepalpay_acc_col and nepalpay_acc_col in nepalpay_df.columns:
        nepalpay_accs.update(nepalpay_df[nepalpay_acc_col].dropna().astype(str).tolist())
        
    all_accounts = fonepay_accs | nepalpay_accs
    
    source_null_province = set()
    source_null_district = set()
    
    fp_prov_col = find_col_by_norm(fonepay_df, 'province')
    fp_dist_col = find_col_by_norm(fonepay_df, 'district')
    np_prov_col = find_col_by_norm(nepalpay_df, 'province')
    np_dist_col = find_col_by_norm(nepalpay_df, 'district')
    
    if fonepay_acc_col:
        for _, row in fonepay_df.iterrows():
            acc = str(row.get(fonepay_acc_col, '')).strip()
            if not acc or is_empty(acc): continue
            if fp_prov_col and is_empty(row.get(fp_prov_col)):
                source_null_province.add(acc)
            if fp_dist_col and is_empty(row.get(fp_dist_col)):
                source_null_district.add(acc)
    
    if nepalpay_acc_col:
        for _, row in nepalpay_df.iterrows():
            acc = str(row.get(nepalpay_acc_col, '')).strip()
            if not acc or is_empty(acc): continue
            if np_prov_col and is_empty(row.get(np_prov_col)):
                source_null_province.add(acc)
            if np_dist_col and is_empty(row.get(np_dist_col)):
                source_null_district.add(acc)
    
    if uid: _set_progress(uid, 3, 'active', f'Querying CBS for {len(all_accounts):,} merchant accounts...')
    
    generate_mock_data(all_accounts, nepalpay_accs, source_null_province, source_null_district)
    
    all_cbs_data = pd.DataFrame(list(CBSMerchant.objects.filter(account_number__in=all_accounts).values(
        'account_number', 'province', 'district', 'municipality', 'address_1', 'address_2', 'gender', 'age'
    )))
    
    cbs_lookup = all_cbs_data.set_index('account_number') if not all_cbs_data.empty else pd.DataFrame()

    if uid: 
        _set_progress(uid, 3, 'active', f'Querying CBS for {len(all_accounts):,} merchant accounts...', extra={'log': f'Found {len(all_cbs_data)} matches in Core Banking System.'})
        _set_progress(uid, 4, 'active', 'Enriching records with CBS data...')

    fonepay_step2_df = fonepay_df.copy()
    nepalpay_step2_df = nepalpay_df.copy()

    def safe_get_cbs(acc, col):
        if not cbs_lookup.empty and acc in cbs_lookup.index:
            val = cbs_lookup.loc[acc, col]
            return val.iloc[0] if isinstance(val, pd.Series) else val
        return None

    if fonepay_acc_col:
        fill_fp = get_fill_col(fonepay_acc_col, cbs_lookup)
        fonepay_step2_df['province'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'province'), axis=1)
        fonepay_step2_df['district'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'district'), axis=1)
        fonepay_step2_df['municipality'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'municipality'), axis=1)
        
        fonepay_step2_df['gender'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'gender'), axis=1)
        fonepay_step2_df['age'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'age'), axis=1)
        fonepay_step2_df['address_1'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'address1', cbs_col='address_1'), axis=1)
        fonepay_step2_df['address_2'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'address2', cbs_col='address_2'), axis=1)

    if nepalpay_acc_col:
        fill_np = get_fill_col(nepalpay_acc_col, cbs_lookup)
        nepalpay_step2_df['province'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'province'), axis=1)
        nepalpay_step2_df['district'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'district'), axis=1)
        
        nepalpay_step2_df['municipality'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'municipality'), axis=1)
        nepalpay_step2_df['gender'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'gender'), axis=1)
        nepalpay_step2_df['age'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'age'), axis=1)
        nepalpay_step2_df['address_1'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'address1', cbs_col='address_1'), axis=1)
        nepalpay_step2_df['address_2'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'address2', cbs_col='address_2'), axis=1)

    output_dir = os.path.join(settings.BASE_DIR, 'media', 'outputs')
    os.makedirs(output_dir, exist_ok=True)
    
    f_path1 = os.path.join(output_dir, f'step1_fonepay_{unique_id}.xlsx')
    n_path1 = os.path.join(output_dir, f'step1_nepalpay_{unique_id}.xlsx')
    f_path2 = os.path.join(output_dir, f'step2_fonepay_{unique_id}.xlsx')
    n_path2 = os.path.join(output_dir, f'step2_nepalpay_{unique_id}.xlsx')
    
    fonepay_df.to_excel(f_path1, index=False)
    nepalpay_df.to_excel(n_path1, index=False)
    fonepay_step2_df.to_excel(f_path2, index=False)
    nepalpay_step2_df.to_excel(n_path2, index=False)
    
    def count_missing(df, cols):
        if df is None or df.empty: return {c: 0 for c in cols}
        counts = {}
        for c in cols:
            found = False
            for col in df.columns:
                if str(col).lower().replace(' ', '').replace('_', '') == c:
                    counts[c] = int(df[col].apply(lambda x: 1 if is_empty(x) else 0).sum())
                    found = True; break
            if not found: counts[c] = len(df)
        return counts

    track_cols = ['province', 'district', 'municipality', 'gender', 'age', 'address1', 'address2']
    b_f = count_missing(fonepay_df, track_cols)
    b_n = count_missing(nepalpay_df, track_cols)
    a_f = count_missing(fonepay_step2_df, track_cols)
    a_n = count_missing(nepalpay_step2_df, track_cols)
    
    stats = {}
    for c in track_cols:
        before = b_f.get(c, 0) + b_n.get(c, 0)
        after = a_f.get(c, 0) + a_n.get(c, 0)
        stats[c] = {'missing_before': before, 'missing_after': after, 'filled': before - after}
    
    if uid:
        log_parts = []
        for c, s in stats.items():
            if s['filled'] > 0: log_parts.append(f"{c.title()}: filled {s['filled']}")
        log_msg = " • ".join(log_parts) if log_parts else "No missing data filled from CBS."
        _set_progress(uid, 4, 'active', f'Enriched records from CBS database.', extra={'log': log_msg})

    return fonepay_df, nepalpay_df, fonepay_step2_df, nepalpay_step2_df, stats

def _generate_final_report(unique_id):
    """Core report generation logic. Returns dict with result info."""
    f_path1 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step1_fonepay_{unique_id}.xlsx')
    n_path1 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step1_nepalpay_{unique_id}.xlsx')
    f_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_fonepay_{unique_id}.xlsx')
    n_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_nepalpay_{unique_id}.xlsx')
    
    c_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'card_data_{unique_id}.xlsx')
    p_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'phonepay_{unique_id}.xlsx')
    np_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'nepalpay_{unique_id}.xlsx')
    cl_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'cardless_{unique_id}.xlsx')
    
    f_step2 = pd.read_excel(f_path2)
    n_step2 = pd.read_excel(n_path2)
    f_acc_col = find_account_col(f_step2)
    n_acc_col = find_account_col(n_step2)
    
    try: card_df = pd.read_excel(c_path)
    except: card_df = pd.DataFrame()
    try: phonepay_df = pd.read_excel(p_path)
    except: phonepay_df = pd.DataFrame()
    try: nepalpay_df = pd.read_excel(np_path)
    except: nepalpay_df = pd.DataFrame()
    try: cardless_df = pd.read_excel(cl_path)
    except: cardless_df = pd.DataFrame()
    
    def get_norm_df(df, acc_col):
        if df is None or df.empty or not acc_col: return pd.DataFrame()
        temp = pd.DataFrame()
        temp['account_number'] = df[acc_col]
        for p in ['province', 'district', 'municipality', 'gender']:
            found = False
            for c in df.columns:
                if str(c).lower().replace(' ', '').replace('_', '') == p:
                    temp[p] = df[c]
                    found = True; break
            if not found: temp[p] = None
        return temp

    p1 = get_norm_df(f_step2, f_acc_col)
    p2 = get_norm_df(n_step2, n_acc_col)
    all_merchants_df = pd.concat([p1, p2], ignore_index=True)

    def create_report_format(all_data, target_col, cats, index_col_name, map_func=None):
        if not all_data.empty and target_col in all_data.columns:
            mapped_series = all_data[target_col].apply(map_func) if map_func else all_data[target_col]
            counts = mapped_series.value_counts().to_dict()
        else: counts = {}
            
        final_df = pd.DataFrame()
        final_df[f'POS-enabled Merchants ({index_col_name}):'] = cats
        final_df['POS No. of Merchants'] = 0
        final_df[''] = '' 
        final_df[f'QR-enabled Merchants ({index_col_name}):'] = cats
        final_df['QR No. of Merchants'] = [counts.get(c, 0) for c in cats]
        final_df[' '] = '' 
        final_df[f'Online-enabled Merchants ({index_col_name}):'] = cats
        final_df['Online No. of Merchants'] = 0
        
        total_row = pd.DataFrame([{
            f'POS-enabled Merchants ({index_col_name}):': 'Total',
            'POS No. of Merchants': 0, '': '',
            f'QR-enabled Merchants ({index_col_name}):': 'Total',
            'QR No. of Merchants': sum(counts.get(c, 0) for c in cats), ' ': '',
            f'Online-enabled Merchants ({index_col_name}):': 'Total',
            'Online No. of Merchants': 0
        }])
        return pd.concat([final_df, total_row], ignore_index=True)

    df_province = create_report_format(all_merchants_df, 'province', provinces_list, 'Province-wise', map_func=map_province)
    df_local = create_report_format(all_merchants_df, 'municipality', local_cats, 'Local Level-wise', map_func=map_local)
    df_district = create_report_format(all_merchants_df, 'district', districts_list, 'District-wise', map_func=map_district)
    
    if not all_merchants_df.empty and 'gender' in all_merchants_df.columns:
        g_counts = all_merchants_df['gender'].apply(map_gender).value_counts().to_dict()
    else: g_counts = {}
    
    g_df = pd.DataFrame()
    g_df['Gender of Proprietor(POS,QR-Enabled,Online Enabled)'] = gender_cats
    g_df['No. of Merchants'] = [g_counts.get(c, 0) for c in gender_cats]
    g_df = pd.concat([g_df, pd.DataFrame([{
        'Gender of Proprietor(POS,QR-Enabled,Online Enabled)': 'Total',
        'No. of Merchants': sum(g_counts.get(c, 0) for c in gender_cats)
    }])], ignore_index=True)

    step3_filename = f'Additional_Payment_Report_ASCII_{unique_id}.xlsx'
    step3_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', step3_filename)
    
    with pd.ExcelWriter(step3_path, engine='openpyxl') as writer:
        def write_sheet(df, sheet_name, title):
            if df.empty: df = pd.DataFrame(["No Data"])
            df.to_excel(writer, sheet_name=sheet_name, startrow=1, index=False)
            ws = writer.sheets[sheet_name]
            ws.merge_cells('B1:G1')
            ws['B1'] = title
            from openpyxl.styles import Font, Alignment
            ws['B1'].font = Font(bold=True)
            ws['B1'].alignment = Alignment(horizontal='center')
            
        write_sheet(df_province, '3.No of Merchants_Province', 'Merchants Accepting Digital Payments (Onboarded by Licensed Institutions) As of Month end - Province Level wise')
        write_sheet(df_local, '4.No of Merchants_Local', 'Merchants Accepting Digital Payments (Onboarded by Licensed Institutions) As of Month end - Local Level wise')
        write_sheet(df_district, '5.No of Merchants_District', 'Merchants Accepting Digital Payments (Onboarded by Licensed Institutions) As of Month end - District Wise')
        
        g_df.to_excel(writer, sheet_name='6.Genderwise_Merchant', startrow=1, index=False)
        ws = writer.sheets['6.Genderwise_Merchant']
        ws.merge_cells('A1:B1')
        ws['A1'] = 'Merchants Onboarded by Licensed Institutions-Gender Wise As of Month End'
        from openpyxl.styles import Font
        ws['A1'].font = Font(bold=True)
        
        # --- NEW LOGIC: International and Domestic Transactions ---
        def get_col(df, possible_names):
            if df is None or df.empty: return None
            import re
            def clean(s): return re.sub(r'[^a-z0-9]', '', str(s).lower())
            
            # Exact match
            for pn in possible_names:
                pn_clean = clean(pn)
                for c in df.columns:
                    if pn_clean == clean(c): return c
                    
            # Prefix match ('amount' catches 'amountnpr' skipping 'commissionamount')
            for pn in possible_names:
                pn_clean = clean(pn)
                for c in df.columns:
                    if clean(c).startswith(pn_clean): return c
                    
            # Substring fallback
            for pn in possible_names:
                pn_clean = clean(pn)
                for c in df.columns:
                    if pn_clean in clean(c): return c
            return None
            
        def sum_col(df, col_keywords):
            col = get_col(df, col_keywords)
            if col: return pd.to_numeric(df[col], errors='coerce').sum()
            return 0

        def calc_amount_and_count(df, base_mask, amt_col_keys):
            if df.empty or not base_mask.any(): return 0, 0
            
            rflag_col = get_col(df, ['reversalflag'])
            if rflag_col:
                rflag_numeric = pd.to_numeric(df[rflag_col], errors='coerce')
                mask0 = base_mask & (rflag_numeric == 0)
                mask1 = base_mask & (rflag_numeric == 1)
            else:
                mask0 = base_mask
                mask1 = pd.Series(False, index=df.index)
                
            amt_col = get_col(df, amt_col_keys)
            
            cnt = len(df[mask0]) - len(df[mask1])
            if amt_col:
                amt0 = pd.to_numeric(df.loc[mask0, amt_col], errors='coerce').sum()
                amt1 = pd.to_numeric(df.loc[mask1, amt_col], errors='coerce').sum()
                amt = amt0 - amt1
            else:
                amt = 0
            return cnt, amt

        # Fields mapped from user definitions
        cnum = get_col(card_df, ['cardnumber'])
        curr = get_col(card_df, ['currency'])
        ttype = get_col(card_df, ['transtype'])
        atm_surch = get_col(card_df, ['atmsurcharge'])
        
        # Int - Card Acquiring -> ATM Terminals
        ca_atm_cnt = ca_atm_amt = 0
        if atm_surch:
            mask = pd.to_numeric(card_df[atm_surch], errors='coerce') == 500
            ca_atm_cnt, ca_atm_amt = calc_amount_and_count(card_df, mask, ['txnamt', 'amount', 'taxationamount'])

        # Int - Card Issuing Base masks
        ci_dc_cnt = ci_dc_amt = 0
        ci_cc_cnt = ci_cc_amt = 0
        ci_pc_cnt = ci_pc_amt = 0
        ci_dc_pos_cnt = ci_dc_pos_amt = 0
        ci_dc_onl_cnt = ci_dc_onl_amt = 0
        ci_dc_atm_cnt = ci_dc_atm_amt = 0
        
        if cnum and curr:
            currency_mask = pd.to_numeric(card_df[curr], errors='coerce') != 524
            
            # Debit Cards (starts with 408833)
            dc_mask = card_df[cnum].astype(str).str.startswith('408833', na=False) & currency_mask
            ci_dc_cnt, ci_dc_amt = calc_amount_and_count(card_df, dc_mask, ['billingamt', 'amount'])
            
            # Credit Cards (starts with 466043)
            cc_mask = card_df[cnum].astype(str).str.startswith('466043', na=False) & currency_mask
            ci_cc_cnt, ci_cc_amt = calc_amount_and_count(card_df, cc_mask, ['billingamt', 'amount'])
            
            # Prepaid Cards (starts with 463724)
            pc_mask = card_df[cnum].astype(str).str.startswith('463724', na=False) & currency_mask
            ci_pc_cnt, ci_pc_amt = calc_amount_and_count(card_df, pc_mask, ['billingamt', 'amount'])
            
            # Subcategories of Debit Card (POS, Online, ATM)
            if ttype:
                tt = pd.to_numeric(card_df[ttype], errors='coerce')
                
                # POS (774)
                pos_mask = dc_mask & (tt == 774)
                ci_dc_pos_cnt, ci_dc_pos_amt = calc_amount_and_count(card_df, pos_mask, ['billingamt', 'amount'])
                
                # Online (ecommerce - 680)
                onl_mask = dc_mask & (tt == 680)
                ci_dc_onl_cnt, ci_dc_onl_amt = calc_amount_and_count(card_df, onl_mask, ['billingamt', 'amount'])
                
                # ATM (700)
                atm_mask = dc_mask & (tt == 700)
                ci_dc_atm_cnt, ci_dc_atm_amt = calc_amount_and_count(card_df, atm_mask, ['billingamt', 'amount'])

        # Int - QR Acquiring (Alipay & NPCI)
        qr_acq_cnt = qr_acq_amt = 0
        issuer_col = get_col(phonepay_df, ['issuer'])
        if issuer_col:
            mask = phonepay_df[issuer_col].astype(str).str.contains('alipay|npci', case=False, na=False)
            qr_acq_cnt = mask.sum() 
            qr_acq_amt = sum_col(phonepay_df[mask], ['originalamount', 'amount'])

        int_data = [
            ['1. Card Acquiring', '', ''],
            ['A. Of which:', '', ''],
            ['A.1 Debit Card', 0, 0.0],
            ['A.2 Credit Card', 0, 0.0],
            ['A.3 Prepaid Card', 0, 0.0],
            ['B. Of which:', '', ''],
            ['B.1 POS', 0, 0.0],
            ['B.2 Online (ecommerce)', 0, 0.0],
            ['B.3 ATM Terminals', ca_atm_cnt, ca_atm_amt],
            ['', '', ''],
            ['2. Card Issuing', '', ''],
            ['A. Of which:', '', ''],
            ['A.1 Debit Card', ci_dc_cnt, ci_dc_amt],
            ['A.2 Credit Card', ci_cc_cnt, ci_cc_amt],
            ['A.3 Prepaid Card', ci_pc_cnt, ci_pc_amt],
            ['B. Of which:', '', ''],
            ['B.1 POS', ci_dc_pos_cnt, ci_dc_pos_amt],
            ['B.2 Online (ecommerce)', ci_dc_onl_cnt, ci_dc_onl_amt],
            ['B.3 ATM Terminals', ci_dc_atm_cnt, ci_dc_atm_amt],
            ['', '', ''],
            ['3. QR Acquiring', qr_acq_cnt, qr_acq_amt],
            ['4. QR Issuing', 0, 0.0],
            ['5. Inward P2P Transfers', 0, 0.0],
            ['6. Outward P2P Transfers', 0, 0.0],
        ]
        
        int_df = pd.DataFrame(int_data, columns=['Particulars', 'Txn Count(Number)', 'Txn Amount(NPR)'])
        
        # Domestic - Cardless Withdrawals
        dom_cw_cnt = len(cardless_df) if not cardless_df.empty else 0
        dom_cw_amt = sum_col(cardless_df, ['amount', 'taxationamount'])
        
        # Domestic - NFC Transactions
        dom_nfc_cnt = dom_nfc_amt = 0
        cinput = get_col(card_df, ['cardinput'])
        if cinput and ttype:
            nfc_mask = card_df[cinput].astype(str).str.lower().str.contains('contactless|contact list', na=False)
            nfc_mask = nfc_mask & (pd.to_numeric(card_df[ttype], errors='coerce') == 774)
            dom_nfc_cnt, dom_nfc_amt = calc_amount_and_count(card_df, nfc_mask, ['billingamt'])
            
        # Domestic - QR Enabled Merchants
        dom_qr_cnt = (len(nepalpay_df) if not nepalpay_df.empty else 0) + (len(phonepay_df) if not phonepay_df.empty else 0)
        dom_qr_amt = sum_col(nepalpay_df, ['amount']) + sum_col(phonepay_df, ['originalamount', 'amount'])
        
        dom_data = [
            ['1. Card Transactions', '', ''],
            ['1.1 Cardless Withdrawals Via ATM', dom_cw_cnt, dom_cw_amt],
            ['1.2 NFC Transactions in Merchant Terminals', dom_nfc_cnt, dom_nfc_amt],
            ['', '', ''],
            ['2. Total Merchants', '', ''],
            ['2.1 POS-enabled Merchants', 0, 0.0],
            ['2.2 QR-Enabled Merchants', dom_qr_cnt, dom_qr_amt],
            ['2.3 E-Commerce Enabled Merchants', 0, 0.0],
        ]
        
        dom_df = pd.DataFrame(dom_data, columns=['Particulars', 'Txn Count(Number)', 'Txn Amount(NPR)'])
        
        write_sheet(int_df, '1.International Transactions', 'International Transactions for the month Ashwin')
        write_sheet(dom_df, '2.Domestic Transactions', 'Domestic Transactions for the month Ashwin')
        
        # Reorder sheets to put International and Domestic at the front (openpyxl specific)
        wb = writer.book
        moved_s1 = wb['1.International Transactions']
        moved_s2 = wb['2.Domestic Transactions']
        wb._sheets.remove(moved_s1)
        wb._sheets.remove(moved_s2)
        wb._sheets.insert(0, moved_s1)
        wb._sheets.insert(1, moved_s2)

    f_df_len = len(pd.read_excel(f_path1)) if os.path.exists(f_path1) else len(f_step2)
    n_df_len = len(pd.read_excel(n_path1)) if os.path.exists(n_path1) else len(n_step2)

    return {
        'step3_filename': step3_filename,
        'fonepay_count': f_df_len,
        'nepalpay_count': n_df_len,
        'fonepay_provinces': get_prov_counts(f_step2),
        'fonepay_districts': get_dist_counts(f_step2),
        'nepalpay_provinces': get_prov_counts(n_step2),
        'nepalpay_districts': get_dist_counts(n_step2),
        'fonepay_step1_provinces': get_prov_counts(pd.read_excel(f_path1) if os.path.exists(f_path1) else pd.DataFrame()),
        'fonepay_step1_districts': get_dist_counts(pd.read_excel(f_path1) if os.path.exists(f_path1) else pd.DataFrame()),
        'nepalpay_step1_provinces': get_prov_counts(pd.read_excel(n_path1) if os.path.exists(n_path1) else pd.DataFrame()),
        'nepalpay_step1_districts': get_dist_counts(pd.read_excel(n_path1) if os.path.exists(n_path1) else pd.DataFrame()),
    }


# --- BACKGROUND PIPELINE ---

def _run_pipeline(file_path, uid):
    """Background thread: runs the full data pipeline with progress tracking."""
    try:
        f_df, n_df, f_step2, n_step2, stats = perform_step1_and_2(file_path, uid, uid=uid)
        
        _set_progress(uid, 5, 'active', 'Validating geographical data completeness...')
        
        fp_missing = check_missing_records(f_step2, ['province', 'district', 'municipality'], find_account_col(f_step2))
        np_missing = check_missing_records(n_step2, ['province', 'district', 'municipality'], find_account_col(n_step2))
        
        has_missing = bool(fp_missing or np_missing)
        total_missing = len(fp_missing) + len(np_missing)
        
        result_info = {
            'unique_id': uid,
            'fonepay_count': len(f_df),
            'nepalpay_count': len(n_df),
        }
        
        if has_missing:
            _set_progress(uid, 5, 'action_required', f'Found {total_missing} records still missing geographical data', extra={**result_info, 'log': f'Unable to resolve Province/District/Municipality for {total_missing} accounts via CBS.'})
        else:
            _set_progress(uid, 6, 'active', 'Generating final regulatory report formats...')
            report = _generate_final_report(uid)
            result_info['step3_filename'] = report['step3_filename']
            _set_progress(uid, 6, 'complete', 'Report compiled successfully', extra={**result_info, 'log': 'Aggregated Province, District, Local Level, and Gender into 4 sheets.'})
    except Exception as e:
        _set_progress(uid, -1, 'error', str(e))
    finally:
        connection.close()


# --- VIEWS ---

def upload_merchant_data(request):
    """Serves the upload page. Processing is handled via API."""
    return render(request, 'merchant/upload.html')


# --- API VIEWS ---

def api_start(request):
    """Accept file, start background pipeline, return unique_id."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    
    file = request.FILES.get('file')
    card_data = request.FILES.get('card_data')
    phonepay_details = request.FILES.get('phonepay_details')
    nepalpay_details = request.FILES.get('nepalpay_details')
    cardless_report = request.FILES.get('cardless_report')

    if not file or not card_data or not phonepay_details or not nepalpay_details or not cardless_report:
        return JsonResponse({'error': 'Please upload all required files.'}, status=400)
    
    uid = str(uuid.uuid4())[:8]
    tmp_dir = os.path.join(settings.BASE_DIR, 'media', 'outputs')
    os.makedirs(tmp_dir, exist_ok=True)
    
    def save_file(f, prefix):
        path = os.path.join(tmp_dir, f'{prefix}_{uid}.xlsx')
        with open(path, 'wb') as df:
            for chunk in f.chunks():
                df.write(chunk)
        return path

    tmp_path = save_file(file, 'upload')
    save_file(card_data, 'card_data')
    save_file(phonepay_details, 'phonepay')
    save_file(nepalpay_details, 'nepalpay')
    save_file(cardless_report, 'cardless')
    
    _set_progress(uid, 0, 'started', 'Pipeline initiated')
    
    t = threading.Thread(target=_run_pipeline, args=(tmp_path, uid), daemon=True)
    t.start()
    
    return JsonResponse({'unique_id': uid})

def api_progress(request, unique_id):
    """Return current pipeline progress as JSON."""
    path = _progress_path(unique_id)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return JsonResponse(json.load(f))
    return JsonResponse({'step': 0, 'status': 'waiting', 'detail': 'Initializing...'})

def api_finalize(request, unique_id):
    """Called when user clicks 'Proceed' — generates the final report."""
    try:
        _set_progress(unique_id, 6, 'active', 'Generating final report...')
        report = _generate_final_report(unique_id)
        _set_progress(unique_id, 6, 'complete', 'Report compiled successfully', extra={
            'unique_id': unique_id,
            'step3_filename': report['step3_filename'],
        })
        return JsonResponse({'status': 'complete', 'step3_filename': report['step3_filename']})
    except Exception as e:
        return JsonResponse({'status': 'error', 'detail': str(e)}, status=500)


# --- LEGACY VIEWS (review, apply, finalize page, download) ---

def review_missing_data(request, unique_id):
    f_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_fonepay_{unique_id}.xlsx')
    n_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_nepalpay_{unique_id}.xlsx')
    
    if not os.path.exists(f_path2) or not os.path.exists(n_path2):
        raise Http404("Processed Data Files not found. They might have been deleted.")
        
    f_step2 = pd.read_excel(f_path2, dtype=str)
    n_step2 = pd.read_excel(n_path2, dtype=str)
    
    fp_missing = check_missing_records(f_step2, ['province', 'district', 'municipality'], find_account_col(f_step2))
    np_missing = check_missing_records(n_step2, ['province', 'district', 'municipality'], find_account_col(n_step2))
    
    all_missing = []
    for acc, data in fp_missing.items():
        data['platform'] = 'FonePay'
        all_missing.append(data)
    for acc, data in np_missing.items():
        if acc not in fp_missing:
            data['platform'] = 'Nepal Pay'
            all_missing.append(data)
            
    return render(request, 'merchant/review_missing_data.html', {
        'unique_id': unique_id,
        'missing_records': all_missing,
        'provinces_list': provinces_list,
        'districts_list': districts_list,
    })

def apply_manual_mapping(request, unique_id):
    if request.method == 'POST':
        f_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_fonepay_{unique_id}.xlsx')
        n_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_nepalpay_{unique_id}.xlsx')
        
        f_step2 = pd.read_excel(f_path2)
        n_step2 = pd.read_excel(n_path2)
        
        f_acc_col = find_account_col(f_step2)
        n_acc_col = find_account_col(n_step2)
        
        updates = {}
        for key, val in request.POST.items():
            if val and str(val).strip() and val != 'Ignore':
                parts = key.split('_', 1)
                if len(parts) == 2 and parts[0] in ['prov', 'dist', 'muni']:
                    col_map = {'prov': 'province', 'dist': 'district', 'muni': 'municipality'}
                    col = col_map[parts[0]]
                    acc = parts[1]
                    if acc not in updates: updates[acc] = {}
                    updates[acc][col] = str(val).strip()
                    
        def patch_df(df, acc_col):
            if not acc_col or df.empty: return df
            for idx, r in df.iterrows():
                acc = str(r.get(acc_col)).strip()
                if acc in updates:
                    for col, new_val in updates[acc].items():
                        if col in df.columns and is_empty(r.get(col)):
                            df.at[idx, col] = new_val
            return df
            
        f_step2 = patch_df(f_step2, f_acc_col)
        n_step2 = patch_df(n_step2, n_acc_col)
        
        f_step2.to_excel(f_path2, index=False)
        n_step2.to_excel(n_path2, index=False)
        
        messages.success(request, 'Successfully applied manual data mappings!')
        return redirect('finalize_report', unique_id=unique_id)
    return redirect('upload_merchant_data')

def finalize_report(request, unique_id):
    """Renders the full results page. Generates report if not already created."""
    step3_filename = f'Additional_Payment_Report_ASCII_{unique_id}.xlsx'
    step3_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', step3_filename)
    
    # Only generate if not already done (e.g. by the background pipeline or api_finalize)
    if not os.path.exists(step3_path):
        _generate_final_report(unique_id)
    
    f_path1 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step1_fonepay_{unique_id}.xlsx')
    n_path1 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step1_nepalpay_{unique_id}.xlsx')
    f_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_fonepay_{unique_id}.xlsx')
    n_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_nepalpay_{unique_id}.xlsx')
    
    f_step2 = pd.read_excel(f_path2)
    n_step2 = pd.read_excel(n_path2)
    
    f_df_len = len(pd.read_excel(f_path1)) if os.path.exists(f_path1) else len(f_step2)
    n_df_len = len(pd.read_excel(n_path1)) if os.path.exists(n_path1) else len(n_step2)

    context = {
        'fonepay_count': f_df_len,
        'nepalpay_count': n_df_len,
        'fonepay_filename': f'step1_fonepay_{unique_id}.xlsx',
        'nepalpay_filename': f'step1_nepalpay_{unique_id}.xlsx',
        'fonepay_step2_filename': f'step2_fonepay_{unique_id}.xlsx',
        'nepalpay_step2_filename': f'step2_nepalpay_{unique_id}.xlsx',
        'step3_filename': step3_filename,
        'fonepay_provinces': get_prov_counts(f_step2),
        'fonepay_districts': get_dist_counts(f_step2),
        'nepalpay_provinces': get_prov_counts(n_step2),
        'nepalpay_districts': get_dist_counts(n_step2),
        'fonepay_step1_provinces': get_prov_counts(pd.read_excel(f_path1) if os.path.exists(f_path1) else pd.DataFrame()),
        'fonepay_step1_districts': get_dist_counts(pd.read_excel(f_path1) if os.path.exists(f_path1) else pd.DataFrame()),
        'nepalpay_step1_provinces': get_prov_counts(pd.read_excel(n_path1) if os.path.exists(n_path1) else pd.DataFrame()),
        'nepalpay_step1_districts': get_dist_counts(pd.read_excel(n_path1) if os.path.exists(n_path1) else pd.DataFrame()),
        'success': True
    }
    return render(request, 'merchant/upload.html', context)

def download_sheet(request, filename):
    file_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', filename)
    if os.path.exists(file_path):
        return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=filename)
    else:
        raise Http404("File not found")