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
import re
from datetime import date, datetime, timedelta
from .models import CBSMerchant, CleanCBS
from .cbs_source import cbs_lookup as _cbs_source_lookup, cbs_accounts_in_set, normalize_cbs_record
from .country_codes import calculate_age as _calculate_age

# --- HELPER FUNCTIONS ---

def is_empty(val):
    return pd.isna(val) or str(val).strip() == '' or str(val).lower() == 'null'

def is_valid_account_number(acc):
    """Valid CBS account: all digits, at least 15 characters. Rejects names, phone numbers, etc."""
    if not acc:
        return False
    s = str(acc).strip()
    return s.isdigit() and len(s) >= 15

def filter_dataframe(df, requested_cols):
    cols_to_keep = []
    for col in df.columns:
        norm_col = str(col).lower().replace(' ', '').replace('_', '')
        if norm_col in requested_cols:
            cols_to_keep.append(col)
    return df[cols_to_keep] if cols_to_keep else df

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
        
        missing = []
        for c in required_cols:
            val = row.get(c)
            # Re-use mappers below to ensure non-empty strings are actually valid.
            if is_empty(val):
                missing.append(c)
            elif c == 'province' and map_province(val) == 'Unmatched':
                missing.append(c)
            elif c == 'district' and map_district(val) == 'Unmatched':
                missing.append(c)
            elif c == 'municipality' and map_local(val) == 'Unmatched':
                missing.append(c)
            # Gender drops to Company automatically if unknown per logic, so we only flag if strictly IS_EMPTY
            
        if missing:
            if acc not in missing_dict:
                missing_dict[acc] = {
                    'account_number': acc,
                    'address_1': '',  # overwritten from DB in review_missing_data
                    'address_3': '',  # overwritten from DB in review_missing_data
                    'needs_province': 'province' in missing,
                    'needs_district': 'district' in missing,
                    'needs_municipality': 'municipality' in missing,
                    'needs_gender': 'gender' in missing,
                }
            else:
                for m in missing:
                    missing_dict[acc][f'needs_{m}'] = True
    return missing_dict

# --- MAPPERS ---

provinces_list = ['Koshi', 'Madhesh', 'Bagmati', 'Gandaki', 'Lumbini', 'Karnali', 'Sudurpaschim']

# Maps every known province code/name variant → canonical name.
# Handles: 3-digit CBS codes, 1/2-digit short codes, 'State-N' (FonePay payment files),
# 'Province N' / 'N Province' / 'X Province' / 'X Pradesh' formats (NepalPay files),
# spelling variants (Sudurpashchim, MADHEDH PRADESH, BR), and clean full names.
_PROVINCE_MAP = {
    # 3-digit CBS codes
    '001': 'Koshi',        '007': 'Sudurpaschim',
    '002': 'Madhesh',      '006': 'Karnali',
    '003': 'Bagmati',      '005': 'Lumbini',
    '004': 'Gandaki',
    # 2-digit variants
    '01': 'Koshi',  '02': 'Madhesh',  '03': 'Bagmati',  '04': 'Gandaki',
    '05': 'Lumbini', '06': 'Karnali', '07': 'Sudurpaschim',
    # 1-digit variants
    '1': 'Koshi',  '2': 'Madhesh',  '3': 'Bagmati',  '4': 'Gandaki',
    '5': 'Lumbini', '6': 'Karnali', '7': 'Sudurpaschim',
    # Full lowercase names (CBS / manual input)
    'koshi': 'Koshi',        'madhesh': 'Madhesh',
    'bagmati': 'Bagmati',    'gandaki': 'Gandaki',
    'lumbini': 'Lumbini',    'karnali': 'Karnali',
    'sudurpaschim': 'Sudurpaschim',
    # Spelling variants
    'sudurpashchim': 'Sudurpaschim',  # NepalPay spelling
    'madhedhpradesh': 'Madhesh',       # CBS typo
    'madheshpradesh': 'Madhesh',
    # 'State-N' format used by FonePay payment detail files
    'state-1': 'Koshi',   'state-2': 'Madhesh',  'state-3': 'Bagmati',
    'state-4': 'Gandaki', 'state-5': 'Lumbini',  'state-6': 'Karnali',
    'state-7': 'Sudurpaschim',
    # 'Province N' format (NepalPay main file)
    'province1': 'Koshi',   'province2': 'Madhesh',  'province3': 'Bagmati',
    'province4': 'Gandaki', 'province5': 'Lumbini',  'province6': 'Karnali',
    'province7': 'Sudurpaschim',
}

def map_province(p):
    if pd.isna(p) or str(p).strip() == '' or str(p).strip().upper() == 'NULL':
        return 'Unmatched'
    p_str = str(p).strip()
    # 1. Exact lookup
    result = _PROVINCE_MAP.get(p_str)
    if result: return result
    # 2. Lowercase lookup (handles 'Koshi', 'STATE-3', 'Province3', etc.)
    result = _PROVINCE_MAP.get(p_str.lower())
    if result: return result
    # 3. Strip spaces/hyphens then look up ('State-3' → 'state3', 'Province 3' → 'province3')
    p_compact = re.sub(r'[\s\-]', '', p_str.lower())
    result = _PROVINCE_MAP.get(p_compact)
    if result: return result
    # 4. Strip trailing ' Province' / ' Pradesh' suffix ('Bagmati Province' → 'bagmati')
    p_lower = p_str.lower()
    for suffix in (' province', ' pradesh'):
        if p_lower.endswith(suffix):
            base = p_lower[:-len(suffix)].strip()
            result = _PROVINCE_MAP.get(base)
            if result: return result
    # 5. Last resort: extract the single province number 1-7
    m = re.search(r'\b([1-7])\b', p_str)
    if m:
        return _PROVINCE_MAP.get(m.group(1), 'Unmatched')
    return 'Unmatched'

districts_list = [
    "Bhojpur District", "Dhankuta District", "Ilam District", "Jhapa District", "Khotang District", "Morang District", "Okhaldhunga District", "Panchthar District", "Sankhuwasabha District", "Solukhumbu District", "Sunsari District", "Taplejung District", "Tehrathum District", "Udayapur District", "Bara District", "Parsa District", "Rautahat District", "Sarlahi District", "Dhanusha District", "Siraha District", "Mahottari District", "Saptari District", "Sindhuli District", "Ramechhap District", "Dolakha District", "Bhaktapur District", "Dhading District", "Kathmandu District", "Kavrepalanchok District", "Lalitpur District", "Nuwakot District", "Rasuwa District", "Sindhupalchok District", "Chitwan District", "Makwanpur District", "Baglung District", "Gorkha District", "Kaski District", "Lamjung District", "Manang District", "Mustang District", "Myagdi District", "Nawalpur District", "Parbat District", "Syangja District", "Tanahun District", "Arghakhanchi District", "Gulmi District", "Kapilvastu District", "Parasi District", "Palpa District", "Rupandehi District", "Banke District", "Bardiya District", "Dang District", "Pyuthan District", "Rolpa District", "Rukum East District", "Dailekh District", "Dolpa District", "Humla District", "Jajarkot District", "Jumla District", "Kalikot District", "Mugu District", "Rukum West District", "Salyan District", "Surkhet District", "Achham District", "Baitadi District", "Bajhang District", "Bajura District", "Dadeldhura District", "Darchula District", "Doti District", "Kailali District", "Kanchanpur District"
]

# CBS / NepalPay spelling variants → canonical NRB spelling (without ' District' suffix).
_DISTRICT_ALIAS = {
    'chitawan':       'Chitwan',
    'dhanakuta':      'Dhankuta',
    'gorakha':        'Gorkha',
    'kapilbastu':     'Kapilvastu',
    'kavre':          'Kavrepalanchok',
    'kavrepalanchowk':'Kavrepalanchok',
    'mahotari':       'Mahottari',
    'makawanpur':     'Makwanpur',
    'nawalparasi':    'Nawalpur',   # Eastern half after 2015 split; best single mapping
    'panchathar':     'Panchthar',
    'rukum':          'Rukum East', # Ambiguous pre-split name; default to East
    'sindhupalchowk': 'Sindhupalchok',
    'sindhupalchok':  'Sindhupalchok',
    'sunasari':       'Sunsari',
    'terhathum':      'Tehrathum',
    'western rukum':  'Rukum West',
}

def map_district(d):
    if pd.isna(d) or str(d).strip() == '': return 'Unmatched'
    d_str = str(d).strip().title()
    if not d_str.endswith(" District"): d_str += " District"
    if d_str in districts_list: return d_str
    # Try CBS/NepalPay spelling alias
    base_lower = d_str.replace(' District', '').lower()
    alias = _DISTRICT_ALIAS.get(base_lower)
    if alias:
        aliased = alias + ' District'
        if aliased in districts_list: return aliased
    return 'Unmatched'

local_cats = ['MP', 'MC', 'Sub MP', 'RM']
def map_local(m):
    # CBS format: "<Name> MP"     = Metropolitan City     (Mahanagar Palika)
    #             "<Name> MC"     = Municipality           (Nagar Palika)
    #             "<Name> Sub MP" = Sub-Metropolitan City  (Upa-Mahanagar Palika)
    #             "<Name> RM"     = Rural Municipality     (Gaun Palika)
    if pd.isna(m) or str(m).strip() == '': return 'Unmatched'
    m_str = str(m).strip().upper()
    # Sub-Metropolitan must be checked before Metropolitan
    if 'SUB' in m_str: return 'Sub MP'
    # Metropolitan: standalone MP or keyword METRO
    if re.search(r'\bMP\b', m_str) or 'METRO' in m_str: return 'MP'
    # Rural Municipality
    if re.search(r'\bRM\b', m_str) or 'RURAL' in m_str: return 'RM'
    # Municipality: standalone MC, standalone M, or keyword MUN
    if re.search(r'\bMC\b', m_str) or re.search(r'\bM\b', m_str) or 'MUN' in m_str: return 'MC'
    return 'Unmatched'

gender_cats = ['Male', 'Female', 'Others (Gender other than Male and Female)', 'Company']
def map_gender(g):
    if pd.isna(g) or str(g).strip() == '': return 'Company'
    g_str = str(g).strip().upper()
    if g_str in ['M', 'MALE']: return 'Male'
    if g_str in ['F', 'FEMALE']: return 'Female'
    # O/OTHER/OTHERS and everything else → Company. Others row is kept in the
    # report for the NRB format but will always be 0.
    return 'Company'

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

# --- REPORT META (month, year, prepared/submitted by) ---

def _meta_path(uid):
    return os.path.join(settings.BASE_DIR, 'media', 'outputs', f'meta_{uid}.json')

def _save_meta(uid, data):
    with open(_meta_path(uid), 'w', encoding='utf-8') as f:
        json.dump(data, f)

def _load_meta(uid):
    p = _meta_path(uid)
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _set_progress(uid, step, status, detail='', extra=None):
    data = {'step': step, 'status': status, 'detail': detail}
    if extra:
        data.update(extra)
    path = _progress_path(uid)
    with open(path, 'w') as f:
        json.dump(data, f)

# --- BUSINESS LOGIC ---

def _random_dob(min_age=18, max_age=70):
    today = date.today()
    age = random.randint(min_age, max_age)
    return date(today.year - age, 1, 1) + timedelta(days=random.randint(0, 364))


def generate_mock_data(all_accounts, nepalpay_accs=None, source_null_province=None, source_null_district=None):
    """
    No-op. CBSMerchant is populated exclusively from the real CBS export
    (load_cbs_excel command). We never inject placeholder rows — accounts
    absent from CBS will be flagged for manual input at review time.
    """
    pass

def _ensure_clean_cbs(account_numbers):
    """
    Sync CleanCBS from the CBS data source for the given accounts.
    • Only accounts known to CBS get a CleanCBS record.
    • Existing CleanCBS rows: any blank field is backfilled from CBS.
    The CBS source is never written to.
    """
    accs = {str(a) for a in account_numbers if a and str(a).strip()}
    if not accs:
        return

    # Single call to the CBS abstraction — swap cbs_lookup() for API when ready
    cbs_map = _cbs_source_lookup(accs)
    if not cbs_map:
        return

    cbs_accs = list(cbs_map.keys())

    # Split into new vs existing CleanCBS rows
    existing_map = {}
    for chunk in [cbs_accs[i:i+900] for i in range(0, len(cbs_accs), 900)]:
        for obj in CleanCBS.objects.filter(account_number__in=chunk):
            existing_map[obj.account_number] = obj

    # --- Create new CleanCBS records ---
    to_create = []
    for acc in cbs_accs:
        if acc in existing_map:
            continue
        r = cbs_map[acc]
        to_create.append(CleanCBS(
            account_number=acc,
            province=r['province'],
            district=r['district'],
            municipality=r['municipality'],
            address_1=r['address_1'],
            address_3=r['address_3'],
            gender=r['gender'] or '',
            dob=r['dob'],
            country_code=r['country_code'],
        ))
    if to_create:
        CleanCBS.objects.bulk_create(to_create, ignore_conflicts=True)

    # --- Backfill blank fields in existing records ---
    to_update = []
    for acc, obj in existing_map.items():
        r = cbs_map.get(acc)
        if not r:
            continue
        changed = False
        for field in ('province', 'district', 'address_1', 'address_3',
                      'gender', 'dob', 'country_code'):
            current = getattr(obj, field)
            if not current or (isinstance(current, str) and current.strip() == ''):
                new_val = r.get(field)
                if new_val and str(new_val).strip():
                    setattr(obj, field, new_val)
                    changed = True
        if changed:
            to_update.append(obj)

    if to_update:
        CleanCBS.objects.bulk_update(
            to_update,
            ['province', 'district', 'address_1', 'address_3', 'gender', 'dob', 'country_code'],
            batch_size=500,
        )

def perform_step1_and_2(file_or_path, unique_id, uid=None):
    """Core pipeline. Accepts file object or path. If uid is provided, tracks progress."""
    if uid: _set_progress(uid, 1, 'active', 'Reading Excel workbook...')

    try:
        fonepay_df = pd.read_excel(file_or_path, sheet_name='fonepay')
    except Exception:
        raise ValueError(
            "Could not find a sheet named 'fonepay' in the uploaded file. "
            "Please make sure you uploaded the correct Total Merchant file "
            "(e.g. 'TOTAL MERCHANT TILL ASOJ(Nepalpay fonepay).xlsx'), "
            "not the Additional Payment Report."
        )
    try:
        nepalpay_df = pd.read_excel(file_or_path, sheet_name='nepalpay')
    except Exception:
        raise ValueError(
            "Could not find a sheet named 'nepalpay' in the uploaded file. "
            "Please make sure you uploaded the correct Total Merchant file."
        )
    
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

    # Drop rows with invalid account numbers (names, phone numbers, etc.)
    # before any further processing. Invalid = not all-digit or < 15 chars.
    invalid_dropped_count = 0
    if fonepay_acc_col and fonepay_acc_col in fonepay_df.columns:
        initial_f_len = len(fonepay_df)
        fonepay_df = fonepay_df[fonepay_df[fonepay_acc_col].apply(
            lambda x: is_valid_account_number(str(x).strip())
        )].copy()
        invalid_dropped_count += initial_f_len - len(fonepay_df)
        
    if nepalpay_acc_col and nepalpay_acc_col in nepalpay_df.columns:
        initial_n_len = len(nepalpay_df)
        nepalpay_df = nepalpay_df[nepalpay_df[nepalpay_acc_col].apply(
            lambda x: is_valid_account_number(str(x).strip())
        )].copy()
        invalid_dropped_count += initial_n_len - len(nepalpay_df)
        
    if uid and invalid_dropped_count > 0:
        _set_progress(uid, 2, 'active', f'Filtering columns — {len(fonepay_df):,} FonePay + {len(nepalpay_df):,} NepalPay records', extra={'log': f'Dropped {invalid_dropped_count} rows with invalid accounts (not numeric or < 15 chars).'})

    # Rebuild account sets after filtering
    fonepay_accs = set(fonepay_df[fonepay_acc_col].dropna().astype(str).tolist()) if fonepay_acc_col else set()
    nepalpay_accs = set(nepalpay_df[nepalpay_acc_col].dropna().astype(str).tolist()) if nepalpay_acc_col else set()
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

    # 1. Read CBS source directly (CBSMerchant) — no CleanCBS mirror needed
    raw_cbs_map = _cbs_source_lookup(all_accounts)

    # 2. Read CleanCBS corrections only (manual + AI — these are the real "clean" entries)
    corrections_map = {}
    for chunk in [list(all_accounts)[i:i+900] for i in range(0, len(all_accounts), 900)]:
        for r in CleanCBS.objects.filter(account_number__in=chunk).values(
            'account_number', 'province', 'district', 'municipality', 'address_1', 'address_3', 'gender', 'dob'
        ):
            corrections_map[r['account_number']] = r

    # 3. Merge: CBS base + CleanCBS overrides (CleanCBS wins for any non-empty field)
    merged_rows = []
    for acc in all_accounts:
        cbs = raw_cbs_map.get(str(acc), {})
        corr = corrections_map.get(str(acc), {})
        merged_rows.append({
            'account_number': str(acc),
            'province':       corr.get('province')     or cbs.get('province')     or '',
            'district':       corr.get('district')     or cbs.get('district')     or '',
            'municipality':   corr.get('municipality') or cbs.get('municipality') or '',
            'address_1':      corr.get('address_1')    or cbs.get('address_1')    or '',
            'address_3':      corr.get('address_3')    or cbs.get('address_3')    or '',
            'gender':         corr.get('gender')       or cbs.get('gender'),
            'dob':            corr.get('dob')          or cbs.get('dob'),
        })

    all_cbs_data = pd.DataFrame(merged_rows)
    # Derive age dynamically from DOB so there's no stored-age staleness.
    if not all_cbs_data.empty:
        all_cbs_data['age'] = all_cbs_data['dob'].apply(lambda d: _calculate_age(d) if pd.notna(d) else None)

    cbs_lookup = all_cbs_data.set_index('account_number') if not all_cbs_data.empty else pd.DataFrame()

    if uid: 
        _set_progress(uid, 3, 'active', f'Querying CBS for {len(all_accounts):,} merchant accounts...', extra={'log': f'Found {len(all_cbs_data)} matches in Core Banking System.'})
        _set_progress(uid, 4, 'active', 'Enriching records with CBS data...')

    fonepay_step2_df = fonepay_df.copy()
    nepalpay_step2_df = nepalpay_df.copy()

    def safe_get_cbs(acc, col):
        acc_str = str(acc).strip() if pd.notna(acc) else ""
        if not cbs_lookup.empty and acc_str in cbs_lookup.index:
            val = cbs_lookup.loc[acc_str, col]
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
        fonepay_step2_df['address_3'] = fonepay_step2_df.apply(lambda r: fill_fp(r, 'address3', cbs_col='address_3'), axis=1)

    if nepalpay_acc_col:
        fill_np = get_fill_col(nepalpay_acc_col, cbs_lookup)
        nepalpay_step2_df['province'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'province'), axis=1)
        nepalpay_step2_df['district'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'district'), axis=1)
        
        nepalpay_step2_df['municipality'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'municipality'), axis=1)
        nepalpay_step2_df['gender'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'gender'), axis=1)
        nepalpay_step2_df['age'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'age'), axis=1)
        nepalpay_step2_df['address_1'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'address1', cbs_col='address_1'), axis=1)
        nepalpay_step2_df['address_3'] = nepalpay_step2_df.apply(lambda r: fill_np(r, 'address3', cbs_col='address_3'), axis=1)

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
    # Load report meta (month, year, prepared/submitted by) saved at upload time.
    meta = _load_meta(unique_id)
    month_name = meta.get('month', '')
    year_val   = meta.get('year', '')

    f_path1 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step1_fonepay_{unique_id}.xlsx')
    n_path1 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step1_nepalpay_{unique_id}.xlsx')
    f_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_fonepay_{unique_id}.xlsx')
    n_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_nepalpay_{unique_id}.xlsx')
    
    c_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'card_data_{unique_id}.xlsx')
    p_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'phonepay_{unique_id}.xlsx')
    np_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'nepalpay_{unique_id}.xlsx')
    cl_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'cardless_{unique_id}.xlsx')
    
    f_step2 = pd.read_excel(f_path2, dtype=str)
    n_step2 = pd.read_excel(n_path2, dtype=str)
    f_acc_col = find_account_col(f_step2)
    n_acc_col = find_account_col(n_step2)

    # Read step-1 files once — reused for counts + province/district stats at the end
    f_step1 = pd.read_excel(f_path1) if os.path.exists(f_path1) else pd.DataFrame()
    n_step1 = pd.read_excel(n_path1) if os.path.exists(n_path1) else pd.DataFrame()

    try: card_df = pd.read_excel(c_path, dtype=str)
    except: card_df = pd.DataFrame()
    try: phonepay_df = pd.read_excel(p_path, dtype=str)
    except: phonepay_df = pd.DataFrame()
    try: nepalpay_df = pd.read_excel(np_path, dtype=str)
    except: nepalpay_df = pd.DataFrame()
    try:
        # Read once with header=None, detect header row, then re-slice in memory
        _raw = pd.read_excel(cl_path, header=None)
        _hdr = next((i for i, row in _raw.iterrows()
                     if any('amount' in str(v).lower() for v in row.values)), None)
        if _hdr is not None:
            cardless_df = _raw.iloc[_hdr + 1:].copy()
            cardless_df.columns = _raw.iloc[_hdr]
            cardless_df = cardless_df.reset_index(drop=True)
        else:
            cardless_df = _raw.copy()
    except: cardless_df = pd.DataFrame()

    # --- Vectorized CBS Patch for Payment Details ---
    pp_acc_col = find_account_col(phonepay_df)
    np2_acc_col = find_account_col(nepalpay_df)

    # FonePay payment-detail files use a short MERCHANT_ID (6-7 digits) rather than
    # the full CBS account number.  Build a MERCHANT_ID → Account Number bridge from
    # the step-2 FonePay data so CBS gender/province lookups work for those rows.
    if pp_acc_col is None and not phonepay_df.empty and not f_step2.empty:
        fp_mid_s2 = find_col_by_norm(f_step2, 'merchantid')
        fp_acc_s2 = find_account_col(f_step2)
        if fp_mid_s2 and fp_acc_s2:
            mid_to_acc = (
                f_step2[[fp_mid_s2, fp_acc_s2]]
                .dropna()
                .set_index(f_step2[fp_mid_s2].astype(str))[fp_acc_s2]
                .astype(str)
                .to_dict()
            )
            # Try MERCHANT_ID then MERCHANT_IDENTIFIER column names
            for _mid_col in ['MERCHANT_ID', 'MERCHANT_IDENTIFIER']:
                if _mid_col in phonepay_df.columns:
                    phonepay_df['_account_number'] = (
                        phonepay_df[_mid_col].astype(str).map(mid_to_acc)
                    )
                    if phonepay_df['_account_number'].notna().any():
                        pp_acc_col = '_account_number'
                        break

    # NepalPay payment-detail files contain a 'Merchant Account' column, but it can
    # be outdated or inaccurate. Build a Merchant Code -> Account Number bridge from
    # the step-2 NepalPay base data to ensure accurate CBS lookups.
    debug_log = [f"--- DEBUG REPORT for {unique_id} ---"]
    debug_log.append(f"pp_acc_col: {pp_acc_col}, phonepay empty: {phonepay_df.empty}, f_step2 empty: {f_step2.empty}")
    if not nepalpay_df.empty and not n_step2.empty:
        np_mid_s2 = find_col_by_norm(n_step2, 'merchantcode')
        np_acc_s2 = find_account_col(n_step2)
        debug_log.append(f"np_mid_s2: {np_mid_s2}, np_acc_s2: {np_acc_s2}")
        if np_mid_s2 and np_acc_s2:
            np_mid_to_acc = (
                n_step2[[np_mid_s2, np_acc_s2]]
                .dropna()
                .set_index(n_step2[np_mid_s2].astype(str))[np_acc_s2]
                .astype(str)
                .to_dict()
            )
            for _mid_col in ['Merchant Code', 'MERCHANT_CODE', 'merchantcode', 'MerchantCode']:
                if _mid_col in nepalpay_df.columns:
                    _bridged = nepalpay_df[_mid_col].astype(str).map(np_mid_to_acc)
                    debug_log.append(f"Found {_mid_col}. Bridged matches: {_bridged.notna().sum()}")
                    if np2_acc_col and np2_acc_col in nepalpay_df.columns:
                        nepalpay_df['_account_number'] = _bridged.fillna(nepalpay_df[np2_acc_col])
                    else:
                        nepalpay_df['_account_number'] = _bridged
                    
                    if nepalpay_df['_account_number'].notna().any():
                        np2_acc_col = '_account_number'
                        break

    with open(os.path.join(settings.BASE_DIR, 'media', 'outputs', f'debug_log_{unique_id}.txt'), 'w') as f_dbg:
        f_dbg.write("\n".join(debug_log))

    add_accs = set()
    if pp_acc_col: add_accs.update(phonepay_df[pp_acc_col].dropna().astype(str).tolist())
    if np2_acc_col: add_accs.update(nepalpay_df[np2_acc_col].dropna().astype(str).tolist())

    if add_accs:
        # Build lookup: CBSMerchant first, CleanCBS corrections override
        raw_cbs_pay = _cbs_source_lookup(add_accs)
        corr_pay = {}
        for chunk in [list(add_accs)[i:i+900] for i in range(0, len(add_accs), 900)]:
            for r in CleanCBS.objects.filter(account_number__in=chunk).values(
                'account_number', 'province', 'district', 'municipality', 'gender'
            ):
                corr_pay[r['account_number']] = r

        merged_pay = []
        for acc in add_accs:
            cbs  = raw_cbs_pay.get(str(acc), {})
            corr = corr_pay.get(str(acc), {})
            merged_pay.append({
                'account_number': str(acc),
                'province':     corr.get('province')     or cbs.get('province')     or '',
                'district':     corr.get('district')     or cbs.get('district')     or '',
                'municipality': corr.get('municipality') or cbs.get('municipality') or '',
                'gender':       corr.get('gender')       or cbs.get('gender'),
            })
        all_cbs_add = pd.DataFrame(merged_pay)
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
        patch_df_vectorized(nepalpay_df, np2_acc_col)
    # ------------------------------------------------
    
    def get_norm_df(df, acc_col):
        if df is None or df.empty or not acc_col: return pd.DataFrame()
        temp = pd.DataFrame()
        temp['account_number'] = df[acc_col]
        for p in ['province', 'district', 'municipality', 'gender']:
            # Use LAST matching column so the enriched lowercase column (added at
            # the end of step-2 enrichment) wins over the original capitalized one.
            matched_col = None
            for c in df.columns:
                if str(c).lower().replace(' ', '').replace('_', '') == p:
                    matched_col = c
            temp[p] = df[matched_col] if matched_col is not None else None
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

    # --- Generate Sheets 7 to 10 for Payment Details ---
    def get_col_txns(df, possible_names):
        if df is None or df.empty: return None
        import re
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

    # Map functions keyed by field name — used to validate which column value is usable.
    _geo_map_funcs = {
        'province':     map_province,
        'district':     map_district,
        'municipality': map_local,
    }

    def _is_empty_series(s):
        return s.isna() | (s.astype(str).str.strip() == '') | (s.astype(str).str.lower() == 'null')

    def get_norm_df_with_amount(df, acc_col, amt_cols):
        """
        Build a normalised per-transaction DataFrame.

        Priority chain for geo fields:
          1. Original file value maps successfully → keep it
          2. CBS-patched column maps successfully → use it
          3. Direct lookup from add_lookup (CBS+CleanCBS merged data) → use it
          4. All fail → stays empty (manual review catches it)
        """
        if df is None or df.empty: return pd.DataFrame()
        temp = pd.DataFrame()
        temp['account_number'] = df[acc_col].astype(str) if acc_col and acc_col in df.columns else None

        for p in ['province', 'district', 'municipality', 'gender']:
            # Collect all columns that normalise to this field name.
            matching_cols = []
            for c in df.columns:
                if str(c).lower().replace(' ', '').replace('_', '') == p:
                    matching_cols.append(c)

            if not matching_cols:
                temp[p] = None
            elif len(matching_cols) == 1:
                temp[p] = df[matching_cols[0]].values
            else:
                # Multiple columns: original file column (first) vs CBS-patched column (last).
                orig_col = matching_cols[0]
                cbs_col  = matching_cols[-1]
                orig_vals = df[orig_col]
                cbs_vals  = df[cbs_col]

                mf = _geo_map_funcs.get(p)
                if mf is not None:
                    orig_usable = (~_is_empty_series(orig_vals)) & (orig_vals.apply(mf) != 'Unmatched')
                    cbs_usable  = (~_is_empty_series(cbs_vals))  & (cbs_vals.apply(mf)  != 'Unmatched')
                    result = orig_vals.copy().astype(object)
                    result = result.where(orig_usable, cbs_vals.where(cbs_usable))
                    temp[p] = result
                else:
                    orig_empty = _is_empty_series(orig_vals)
                    temp[p] = orig_vals.where(~orig_empty, cbs_vals)

            # SAFETY NET: fill any remaining NaN/empty directly from add_lookup.
            # This catches cases where patch_df_vectorized didn't transfer CBS data
            # (e.g. column wasn't picked up, format mismatch, or NepalPay with no
            # original geo columns where the single CBS-patched col is still empty).
            if acc_col and not add_lookup.empty and p in add_lookup.columns and temp['account_number'] is not None:
                still_empty = _is_empty_series(temp[p])
                mf = _geo_map_funcs.get(p)
                if mf is not None:
                    still_empty = still_empty | (temp[p].apply(mf) == 'Unmatched')
                if still_empty.any():
                    direct_vals = temp['account_number'].map(add_lookup[p])
                    direct_valid = direct_vals.notna() & (direct_vals.astype(str).str.strip() != '') & (direct_vals.astype(str).str.lower() != 'null')
                    fill_mask = still_empty & direct_valid
                    if fill_mask.any():
                        temp.loc[fill_mask, p] = direct_vals[fill_mask]

        amt_col = get_col_txns(df, amt_cols)
        temp['amount'] = pd.to_numeric(df[amt_col], errors='coerce').fillna(0) if amt_col else 0.0
        return temp

    pp_norm = get_norm_df_with_amount(phonepay_df, pp_acc_col, ['originalamount', 'amount'])
    np_norm = get_norm_df_with_amount(nepalpay_df, np2_acc_col, ['amount'])
    all_payment_df = pd.concat([pp_norm, np_norm], ignore_index=True)

    # ---- DEBUG: trace where transactions are lost ----
    import logging
    _dbg = logging.getLogger('payment_debug')
    _dbg.setLevel(logging.DEBUG)
    if not _dbg.handlers:
        _dbg.addHandler(logging.StreamHandler())
    _dbg.debug(f"=== PAYMENT DEBUG ===")
    _dbg.debug(f"phonepay rows: {len(phonepay_df)}, nepalpay rows: {len(nepalpay_df)}, total: {len(all_payment_df)}")
    # --- NepalPay deep check ---
    if np2_acc_col and np2_acc_col in nepalpay_df.columns:
        _np_accs = nepalpay_df[np2_acc_col].dropna().astype(str).unique()
        _dbg.debug(f"  NEPALPAY unique accounts: {len(_np_accs)}")
        _dbg.debug(f"  NEPALPAY sample account numbers: {list(_np_accs[:5])}")
        _dbg.debug(f"  NEPALPAY account dtype: {nepalpay_df[np2_acc_col].dtype}")
        # How many are in add_lookup?
        _in_lookup = sum(1 for a in _np_accs if a in add_lookup.index)
        _dbg.debug(f"  NEPALPAY accounts in add_lookup: {_in_lookup}/{len(_np_accs)}")
        # How many are in raw CBS?
        _in_cbs = sum(1 for a in _np_accs if str(a) in raw_cbs_pay)
        _dbg.debug(f"  NEPALPAY accounts in raw_cbs_pay: {_in_cbs}/{len(_np_accs)}")
        # Sample add_lookup index
        _dbg.debug(f"  add_lookup index sample: {list(add_lookup.index[:5])}")
        _dbg.debug(f"  add_lookup index dtype: {add_lookup.index.dtype}")
        # Check if province got patched for NepalPay
        if 'province' in nepalpay_df.columns:
            _np_prov_nan = nepalpay_df['province'].isna().sum()
            _dbg.debug(f"  NEPALPAY province NaN AFTER patch: {_np_prov_nan}/{len(nepalpay_df)}")
        else:
            _dbg.debug(f"  NEPALPAY province column NOT FOUND after patch!")
        # For accounts NOT in add_lookup, show them
        _missing_from_lookup = [a for a in _np_accs if a not in add_lookup.index]
        if _missing_from_lookup:
            _dbg.debug(f"  NEPALPAY accounts NOT in add_lookup ({len(_missing_from_lookup)}): {_missing_from_lookup[:5]}")
        # For accounts IN add_lookup but with empty province
        _in_but_empty = [a for a in _np_accs if a in add_lookup.index and (pd.isna(add_lookup.loc[a, 'province']) or str(add_lookup.loc[a, 'province']).strip() in ('', 'None', 'nan', 'null'))]
        _dbg.debug(f"  NEPALPAY accounts in add_lookup but empty province: {len(_in_but_empty)}")
        if _in_but_empty:
            _dbg.debug(f"    sample: {_in_but_empty[:3]}")
    # Final result check
    for _geo in ['province', 'district', 'municipality']:
        if _geo in all_payment_df.columns:
            _mf = {'province': map_province, 'district': map_district, 'municipality': map_local}[_geo]
            _mapped = all_payment_df[_geo].apply(_mf)
            _um = (_mapped == 'Unmatched').sum()
            _dbg.debug(f"  FINAL {_geo}: unmatched={_um}/{len(all_payment_df)}")
    _dbg.debug(f"=== END PAYMENT DEBUG ===")
    # ---- END DEBUG ----

    def create_txns_report_format(all_data, target_col, cats, index_col_name, map_func=None):
        final_df = pd.DataFrame()
        final_df[f'POS-enabled Merchants ({index_col_name}):'] = cats
        final_df['Txn Count(Number)'] = 0
        final_df['Txn Amount(NPR)'] = 0.0
        final_df[''] = ''
        
        if not all_data.empty and target_col in all_data.columns:
            mapped_series = all_data[target_col].apply(map_func) if map_func else all_data[target_col]
            grp = all_data.groupby(mapped_series)
            grouped_counts = grp.size().to_dict()
            grouped_sums = grp['amount'].sum().to_dict()
        else:
            grouped_counts = {}
            grouped_sums = {}
            
        final_df[f'QR-enabled Merchants ({index_col_name}):'] = cats
        final_df['Txn Count(Number) '] = [grouped_counts.get(c, 0) for c in cats]
        final_df['Txn Amount(NPR) '] = [round(grouped_sums.get(c, 0.0), 2) for c in cats]
        final_df[' '] = ''
        
        final_df[f'Online-enabled Merchants ({index_col_name}):'] = cats
        final_df['Txn Count(Number)  '] = 0
        final_df['Txn Amount(NPR)  '] = 0.0
        
        total_row = pd.DataFrame([{
            f'POS-enabled Merchants ({index_col_name}):': 'Total',
            'Txn Count(Number)': 0,
            'Txn Amount(NPR)': 0.0,
            '': '',
            f'QR-enabled Merchants ({index_col_name}):': 'Total',
            'Txn Count(Number) ': sum(grouped_counts.get(c, 0) for c in cats),
            'Txn Amount(NPR) ': round(sum(grouped_sums.get(c, 0.0) for c in cats), 2),
            ' ': '',
            f'Online-enabled Merchants ({index_col_name}):': 'Total',
            'Txn Count(Number)  ': 0,
            'Txn Amount(NPR)  ': 0.0,
        }])
        return pd.concat([final_df, total_row], ignore_index=True)

    def create_txns_gender_format(all_data):
        cats = ['Male', 'Female', 'Others (Gender other than Male and Female)', 'Company']
        final_df = pd.DataFrame()
        final_df['Gender of Proprietor(POS,QR-Enabled,Online Enabled)'] = cats
        
        if not all_data.empty and 'gender' in all_data.columns:
            mapped_series = all_data['gender'].apply(map_gender)
            grp = all_data.groupby(mapped_series)
            grouped_counts = grp.size().to_dict()
            grouped_sums = grp['amount'].sum().to_dict()
        else:
            grouped_counts = {}
            grouped_sums = {}
            
        final_df['Txn Count(Number)'] = [grouped_counts.get(c, 0) for c in cats]
        final_df['Txn Amount(NPR)'] = [round(grouped_sums.get(c, 0.0), 2) for c in cats]
        
        total_row = pd.DataFrame([{
            'Gender of Proprietor(POS,QR-Enabled,Online Enabled)': 'Total',
            'Txn Count(Number)': sum([grouped_counts.get(c, 0) for c in cats]),
            'Txn Amount(NPR)': round(sum([grouped_sums.get(c, 0.0) for c in cats]), 2)
        }])
        return pd.concat([final_df, total_row], ignore_index=True)

    df_province_pay = create_txns_report_format(all_payment_df, 'province', provinces_list, 'Province-wise', map_func=map_province)
    df_local_pay = create_txns_report_format(all_payment_df, 'municipality', local_cats, 'Local Level-wise', map_func=map_local)
    df_district_pay = create_txns_report_format(all_payment_df, 'district', districts_list, 'District-wise', map_func=map_district)
    g_df_pay = create_txns_gender_format(all_payment_df)
    # ---------------------------------------------------

    step3_filename = f'Additional_Payment_Report_ASCII_{unique_id}.xlsx'
    step3_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', step3_filename)
    
    with pd.ExcelWriter(step3_path, engine='openpyxl') as writer:
        from openpyxl.styles import Font, Alignment, Border, Side
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin'),
        )
        header_font = Font(bold=True)
        center_align = Alignment(horizontal='center')

        def apply_table_format(ws, data_rows, data_cols):
            """Apply black thin borders and bold headers, skipping empty spacer columns."""
            # Identify spacer columns (header is empty or whitespace-only)
            spacer_cols = set()
            for col_idx in range(1, data_cols + 1):
                hdr = ws.cell(row=2, column=col_idx).value
                if hdr is None or str(hdr).strip() == '':
                    spacer_cols.add(col_idx)

            for col_idx in range(1, data_cols + 1):
                if col_idx in spacer_cols:
                    continue
                # Bold + border on header row (row 2)
                cell = ws.cell(row=2, column=col_idx)
                cell.font = header_font
                cell.border = thin_border
                # Borders on data rows
                for row_idx in range(3, 3 + data_rows):
                    ws.cell(row=row_idx, column=col_idx).border = thin_border

            # Auto-fit column widths
            for col_idx in range(1, data_cols + 1):
                if col_idx in spacer_cols:
                    ws.column_dimensions[ws.cell(row=2, column=col_idx).column_letter].width = 3
                    continue
                max_len = 0
                for row_idx in range(2, 3 + data_rows):
                    val = ws.cell(row=row_idx, column=col_idx).value
                    if val is not None:
                        max_len = max(max_len, len(str(val)))
                ws.column_dimensions[ws.cell(row=2, column=col_idx).column_letter].width = max(max_len + 2, 10)

        def write_sheet(df, sheet_name, title):
            if df.empty: df = pd.DataFrame(["No Data"])
            df.to_excel(writer, sheet_name=sheet_name, startrow=1, index=False)
            ws = writer.sheets[sheet_name]
            ws.merge_cells('B1:G1')
            ws['B1'] = title
            ws['B1'].font = header_font
            ws['B1'].alignment = center_align
            apply_table_format(ws, len(df), len(df.columns))
            
        write_sheet(df_province, '3.No of Merchants_Province', 'Merchants Accepting Digital Payments (Onboarded by Licensed Institutions) As of Month end - Province Level wise')
        write_sheet(df_local, '4.No of Merchants_Local', 'Merchants Accepting Digital Payments (Onboarded by Licensed Institutions) As of Month end - Local Level wise')
        write_sheet(df_district, '5.No of Merchants_District', 'Merchants Accepting Digital Payments (Onboarded by Licensed Institutions) As of Month end - District Wise')
        
        g_df.to_excel(writer, sheet_name='6.Genderwise_Merchant', startrow=1, index=False)
        ws = writer.sheets['6.Genderwise_Merchant']
        ws.merge_cells('A1:B1')
        ws['A1'] = 'Merchants Onboarded by Licensed Institutions-Gender Wise As of Month End'
        ws['A1'].font = header_font
        apply_table_format(ws, len(g_df), len(g_df.columns))

        # --- Write Sheets 7 to 10 for Payment Details ---
        _m = month_name or 'the Month'
        write_sheet(df_province_pay, '7. Merchant Txns_Province', f'Province wise Merchant Transactions for the Month {_m}')
        write_sheet(df_district_pay, '8.Merchant Txns_District', f'District wise Transactions of Merchants for the Month {_m}')
        write_sheet(df_local_pay, '9.Merchant Txns_Local', f'Local Level Wise Merchant Transactions for the Month {_m}')

        g_df_pay.to_excel(writer, sheet_name='10. Genderwise_Txn', startrow=1, index=False)
        ws_pay = writer.sheets['10. Genderwise_Txn']
        ws_pay.merge_cells('A1:C1')
        ws_pay['A1'] = f'Transactions of Merchants Onboarded by Licensed Institutions-Gender Wise for the Month {_m}'
        ws_pay['A1'].font = header_font
        apply_table_format(ws_pay, len(g_df_pay), len(g_df_pay.columns))
        # ------------------------------------------------
        
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
        amt_col = get_col(cardless_df, ['amount'])
        if amt_col:
            amt_series = pd.to_numeric(cardless_df[amt_col], errors='coerce')
            dom_cw_cnt = int(amt_series.notna().sum())
            dom_cw_amt = amt_series.sum()
        else:
            dom_cw_cnt = len(cardless_df) if not cardless_df.empty else 0
            dom_cw_amt = 0
        
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

        # --- Sheet 11: Digital Channel Users (Mobile Banking + Connect IPS) ---
        import traceback as _tb
        try:
            from .processors_users import write_sheet_11_from_uid
            write_sheet_11_from_uid(writer, unique_id)
        except Exception as _e:
            # Sheet 11 failure must never break sheets 1-10, but log the full trace.
            print(f"[sheet11] ERROR — sheet omitted: {_e}\n{_tb.format_exc()}")
        # ---------------------------------------------------------------------

        # ── Home Page ──────────────────────────────────────────────────────────
        from openpyxl.styles import Font as _Font, Alignment as _Align, PatternFill as _Fill, Border as _HpBorder, Side as _HpSide
        wb = writer.book
        ws_hp = wb.create_sheet('Home Page')

        # Exact colors from the manual report
        _BG   = _Fill(start_color='F4B083', end_color='F4B083', fill_type='solid')   # orange bg
        _THIN = _HpBorder(left=_HpSide(style='thin'), right=_HpSide(style='thin'),
                          top=_HpSide(style='thin'),  bottom=_HpSide(style='thin'))

        def _hp_cell(row, col, value, bold=False, size=14, halign='left',
                     font_color='000000', bg=True, border=False):
            c = ws_hp.cell(row=row, column=col, value=value)
            c.font = _Font(bold=bold, size=size, color=font_color)
            c.alignment = _Align(horizontal=halign, vertical='center')
            if bg:
                c.fill = _BG
            if border:
                c.border = _THIN
            return c

        # Paint orange background across A:C for all content rows
        for r in range(1, 21):
            for col in range(1, 4):   # A, B, C
                ws_hp.cell(row=r, column=col).fill = _BG

        # Title block  — merged B:C, orange bg
        _hp_cell(1, 2, 'Nepal Rastra Bank',              bold=True, size=18, halign='center', font_color='FF0000')
        _hp_cell(2, 2, 'Additional Reporting Format',    bold=True, size=15, halign='center')
        _hp_cell(3, 2, 'Monthly/Quarterly/Yearly Reports', bold=True, size=11, halign='center')
        ws_hp.merge_cells('B1:C1')
        ws_hp.merge_cells('B2:C2')
        ws_hp.merge_cells('B3:C3')

        # Institution / month / year — label (orange, border), value (orange, border)
        _hp_cell(4, 2, 'Name of the Institution', bold=True,  size=14, halign='right', border=True)
        _hp_cell(4, 3, 'Jyoti Bikash Bank Ltd.',  bold=False, size=12, halign='left',  border=True)
        _hp_cell(5, 2, 'Month',                   bold=False, size=14, halign='right', border=True)
        _hp_cell(5, 3, month_name,                bold=False, size=14, halign='left',  border=True)
        _hp_cell(6, 2, 'Year',                    bold=False, size=14, halign='right', border=True)
        _hp_cell(6, 3, year_val,                  bold=False, size=14, halign='left',  border=True)

        # Prepared by section
        _hp_cell(8, 2, 'Prepared by', bold=True, size=14, halign='center', border=True)
        ws_hp.merge_cells('B8:C8')
        _hp_cell(9,  2, 'Name:',       bold=False, size=14, halign='right', border=True); _hp_cell(9,  3, '', border=True)
        _hp_cell(10, 2, 'Position:',   bold=False, size=14, halign='right', border=True); _hp_cell(10, 3, '', border=True)
        _hp_cell(11, 2, 'Email:',      bold=False, size=14, halign='right', border=True); _hp_cell(11, 3, '', border=True)
        _hp_cell(12, 2, 'Mobile No.:', bold=False, size=14, halign='right', border=True); _hp_cell(12, 3, '', border=True)

        # Submitted by section
        _hp_cell(14, 2, 'Submitted by', bold=True, size=14, halign='center', border=True)
        ws_hp.merge_cells('B14:C14')
        _hp_cell(15, 2, 'Name:',       bold=False, size=14, halign='right', border=True); _hp_cell(15, 3, '', border=True)
        _hp_cell(16, 2, 'Position:',   bold=False, size=14, halign='right', border=True); _hp_cell(16, 3, '', border=True)
        _hp_cell(17, 2, 'Date:',       bold=False, size=14, halign='right', border=True); _hp_cell(17, 3, '', border=True)
        _hp_cell(18, 2, 'Email:',      bold=False, size=14, halign='right', border=True); _hp_cell(18, 3, '', border=True)
        _hp_cell(19, 2, 'Mobile No.:', bold=False, size=14, halign='right', border=True); _hp_cell(19, 3, '', border=True)

        # Column widths & row heights for Home Page
        ws_hp.column_dimensions['A'].width = 3
        ws_hp.column_dimensions['B'].width = 28
        ws_hp.column_dimensions['C'].width = 36
        for r in range(1, 21):
            ws_hp.row_dimensions[r].height = 20

        # ── Glossary ────────────────────────────────────────────────────────────
        ws_gl = wb.create_sheet('Glossary')
        from openpyxl.styles import Border as _Border, Side as _Side
        _gl_thin = _Border(
            left=_Side(style='thin'), right=_Side(style='thin'),
            top=_Side(style='thin'),  bottom=_Side(style='thin'),
        )
        gl_headers = ['S.N.', 'Particulars', 'Sheet No.', 'Definition']
        for ci, h in enumerate(gl_headers, 1):
            c = ws_gl.cell(row=1, column=ci, value=h)
            c.font = _Font(bold=True)
            c.border = _gl_thin
            c.alignment = _Align(horizontal='center', vertical='center', wrap_text=True)

        gl_rows = [
            (1,  'Card Acquiring',
             '1. International Transaction',
             'Refers to transactions from international cards (debit, credit, prepaid) issued by foreign banks, acquired at Merchant terminals onboarded by licensed institutions in Nepal.'),
            (2,  'Card Issuing',
             '1. International Transaction',
             'Refers to transactions from cards (debit, credit, prepaid) issued by Nepalese BFIs, acquired at Merchant terminals outside Nepal.'),
            (3,  'QR Acquiring',
             '1. International Transaction',
             'Refers to transactions, originated from instruments issued by BFIs in other countries, acquired by Nepalese QR merchants in Nepal.'),
            (4,  'QR Issuing',
             '1. International Transaction',
             'Refers to transactions, originated from instruments issued by licensed insitutions in Nepal, acquired by QR merchants in other countries.'),
            (5,  ' Inward P2P Transfers',
             '1. International Transaction',
             'Refers to peer to peer transfer payments received from other countries '),
            (6,  ' Outward P2P Transfers',
             '1. International Transaction',
             'Refers to peer to peer transfer payments made from Nepal to other countries'),
            (7,  'Cardless Withdrawals Via ATM',
             '2. Domestic Transaction',
             'Refers to ATM withdrawals processed using mobile banking application, without using physical cards in the ATM terminal '),
            (8,  'NFC Transactions in Merchant Terminals',
             '2. Domestic Transaction',
             'Refers to card-based transactions at merchant terminals using NFC or tap feature (without entering PIN)'),
            (9,  'POS-enabled Merchants',
             '2. Domestic Transaction',
             'Refers to merchants onboarded by BFIs, accepting digital payments via Point-of-Sale (POS) machines'),
            (10, 'QR-Enabled Merchants',
             '2. Domestic Transaction',
             'Refers to merchants onboarded by licensed institutions, accepting digital payments via QR Codes'),
            (11, 'E-Commerce Enabled Merchants',
             '2. Domestic Transaction',
             'Refers to merchants onboarded by licensed institutions, operating e-commerce platforms/sites and accepting digital payments (checkout) through gateway integrations'),
            (12, ' Merchants Onboarded by Licensed Institutions-Gender Wise As of Month End',
             '6. Genderwise_merchant',
             'Refers to data of merchants onboarded by licensed institutions, categorized based on the gender of the propreitor or the company (if the merchant is a company)'),
            (13, 'Company',
             '6. Genderwise Merchant',
             'Refers to merchants, other than sole propreitorship, onboarded by licensed institutions. For sole proprietorship firms onboarded as merchants, licensed institutions are required to report the gender of the owner. Licensed institutions are required to report merchants other than sole propreitorship as company.'),
            (14, 'Merchants Accepting Digital Payments (Onboarded by Licensed Insititutions) As of Month end ',
             '3.,4.,5.',
             'Refers to cumulative number of merchants onboarded by licensed institutions till the reporting period.'),
            (15, 'Faster payment systems',
             '11. Users',
             'Refers to real-time fast payment systems like connectIPS, issued to customers by licensed institutions.'),
            (16, 'ACH',
             '11. Users',
             'Refers to automated clearing house (ACH) systems, offering bulk debit or credit transfer facilities, extended to customers by banks and financial institutions.'),
        ]
        for ri, (sn, particulars, sheet_no, definition) in enumerate(gl_rows, 2):
            vals = [sn, particulars, sheet_no, definition]
            for ci, v in enumerate(vals, 1):
                c = ws_gl.cell(row=ri, column=ci, value=v)
                c.border = _gl_thin
                c.alignment = _Align(horizontal='left', vertical='center', wrap_text=True)
        # Column widths for Glossary
        ws_gl.column_dimensions['A'].width = 6
        ws_gl.column_dimensions['B'].width = 40
        ws_gl.column_dimensions['C'].width = 24
        ws_gl.column_dimensions['D'].width = 70
        # Row heights for wrapped definition text
        for ri in range(2, len(gl_rows) + 2):
            ws_gl.row_dimensions[ri].height = 40

        # ── Sheet 12: Digital Lending ───────────────────────────────────────────
        ws_dl = wb.create_sheet('12.Digital Lending')
        dl_headers = [
            'S.N.', 'Product Name',
            'Total Number of Outstanding Borrowers as of Month End',
            'Total Number of New Loan Clients for the reporting Month',
            'Total Sanctioned Loan Amount(NPR) in reporting month',
            'Total Sanctioned Loan Amount (NPR) till Month End',
            'Outstanding amount (NPR) till Month End',
            'Non Performing Loans(NPR)-Substandard',
            'Non Performing Loans(NPR)-Doubtful',
            'Non Performing Loans(NPR)-Loss',
            'NPL-Loss (Percentage)',
        ]
        from openpyxl.styles import Border as _Border, Side as _Side

        _dl_thin = _Border(
            left=_Side(style='thin'), right=_Side(style='thin'),
            top=_Side(style='thin'),  bottom=_Side(style='thin'),
        )
        # Title row
        ws_dl.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(dl_headers))
        tc = ws_dl.cell(row=1, column=1, value='Digital Lending')
        tc.font = _Font(bold=True)
        tc.alignment = _Align(horizontal='center')
        # Header row
        for ci, h in enumerate(dl_headers, 1):
            c = ws_dl.cell(row=2, column=ci, value=h)
            c.font = _Font(bold=True)
            c.border = _dl_thin
            c.alignment = _Align(horizontal='center', wrap_text=True)
        # 15 empty data rows with S.N.
        for sn in range(1, 16):
            for ci in range(1, len(dl_headers) + 1):
                c = ws_dl.cell(row=sn + 2, column=ci, value=sn if ci == 1 else None)
                c.border = _dl_thin
        # Column widths
        ws_dl.column_dimensions['A'].width = 6
        ws_dl.column_dimensions['B'].width = 22
        for col_letter in ['C','D','E','F','G','H','I','J','K']:
            ws_dl.column_dimensions[col_letter].width = 18
        ws_dl.row_dimensions[2].height = 40

        # ── Reorder: Home Page → Glossary → International → Domestic → 3-12 ────
        moved_hp = wb['Home Page']
        moved_gl = wb['Glossary']
        moved_s1 = wb['1.International Transactions']
        moved_s2 = wb['2.Domestic Transactions']
        for sheet in [moved_hp, moved_gl, moved_s1, moved_s2]:
            wb._sheets.remove(sheet)
        wb._sheets.insert(0, moved_hp)
        wb._sheets.insert(1, moved_gl)
        wb._sheets.insert(2, moved_s1)
        wb._sheets.insert(3, moved_s2)

    return {
        'step3_filename': step3_filename,
        'fonepay_count': len(f_step1) if not f_step1.empty else len(f_step2),
        'nepalpay_count': len(n_step1) if not n_step1.empty else len(n_step2),
        'fonepay_provinces': get_prov_counts(f_step2),
        'fonepay_districts': get_dist_counts(f_step2),
        'nepalpay_provinces': get_prov_counts(n_step2),
        'nepalpay_districts': get_dist_counts(n_step2),
        'fonepay_step1_provinces': get_prov_counts(f_step1),
        'fonepay_step1_districts': get_dist_counts(f_step1),
        'nepalpay_step1_provinces': get_prov_counts(n_step1),
        'nepalpay_step1_districts': get_dist_counts(n_step1),
    }


# --- BACKGROUND PIPELINE ---

def _check_payment_detail_missing(uid):
    """
    Pre-enrich payment-detail accounts (PhonePay + NepalPay txn files) via CBS.
    Seeds mock CBS for any accounts not yet present, then returns a dict of
    accounts that still have at least one empty field (province/district/
    municipality/gender) keyed by account number.
    """
    output_dir = os.path.join(settings.BASE_DIR, 'media', 'outputs')
    p_path = os.path.join(output_dir, f'phonepay_{uid}.xlsx')
    np_path = os.path.join(output_dir, f'nepalpay_{uid}.xlsx')

    try: phonepay_df_pay = pd.read_excel(p_path)
    except: phonepay_df_pay = pd.DataFrame()
    try: nepalpay_df_pay = pd.read_excel(np_path)
    except: nepalpay_df_pay = pd.DataFrame()

    pp_acc_col = find_account_col(phonepay_df_pay)
    np_acc_col = find_account_col(nepalpay_df_pay)

    # FonePay payment-detail files carry a short MERCHANT_ID, not a full account
    # number.  Build the same MERCHANT_ID → Account bridge used in report generation
    # so that FonePay accounts are included in the CBS pre-check and manual review.
    if pp_acc_col is None and not phonepay_df_pay.empty:
        f_path2 = os.path.join(output_dir, f'step2_fonepay_{uid}.xlsx')
        try:
            f_step2_pre = pd.read_excel(f_path2)
            fp_mid_s2 = find_col_by_norm(f_step2_pre, 'merchantid')
            fp_acc_s2 = find_account_col(f_step2_pre)
            if fp_mid_s2 and fp_acc_s2:
                mid_to_acc_pre = (
                    f_step2_pre[[fp_mid_s2, fp_acc_s2]]
                    .dropna()
                    .set_index(f_step2_pre[fp_mid_s2].astype(str))[fp_acc_s2]
                    .astype(str)
                    .to_dict()
                )
                for _mid_col in ['MERCHANT_ID', 'MERCHANT_IDENTIFIER']:
                    if _mid_col in phonepay_df_pay.columns:
                        phonepay_df_pay['_account_number'] = (
                            phonepay_df_pay[_mid_col].astype(str).map(mid_to_acc_pre)
                        )
                        if phonepay_df_pay['_account_number'].notna().any():
                            pp_acc_col = '_account_number'
                            break
        except Exception:
            pass

    add_accs = set()
    np_payment_accs = set()
    if pp_acc_col and not phonepay_df_pay.empty:
        add_accs.update(phonepay_df_pay[pp_acc_col].dropna().astype(str).tolist())
    if np_acc_col and not nepalpay_df_pay.empty:
        np_accs = set(nepalpay_df_pay[np_acc_col].dropna().astype(str).tolist())
        add_accs.update(np_accs)
        np_payment_accs = np_accs

    if not add_accs:
        return {}

    # Direct CBS lookup + CleanCBS overlay (corrections take priority).
    raw_cbs_pay2 = _cbs_source_lookup(add_accs)
    corr_pay2 = {}
    _SQL_CHUNK2 = 900
    add_accs_list2 = list(add_accs)
    for _i in range(0, len(add_accs_list2), _SQL_CHUNK2):
        _chunk = add_accs_list2[_i:_i + _SQL_CHUNK2]
        for _r in CleanCBS.objects.filter(account_number__in=_chunk).values(
            'account_number', 'province', 'district', 'municipality', 'address_1', 'address_3', 'gender'
        ):
            corr_pay2[_r['account_number']] = _r

    # Only include accounts known to CBS or CleanCBS
    merged_pay2 = []
    for acc in add_accs:
        cbs = raw_cbs_pay2.get(acc, {})
        corr = corr_pay2.get(acc, {})
        if not cbs and not corr:
            continue  # not in CBS at all — skip
        merged_pay2.append({
            'account_number': acc,
            'province':     corr.get('province')     or cbs.get('province')     or '',
            'district':     corr.get('district')     or cbs.get('district')     or '',
            'municipality': corr.get('municipality') or cbs.get('municipality') or '',
            'address_1':    corr.get('address_1')    or cbs.get('address_1')    or '',
            'address_3':    corr.get('address_3')    or cbs.get('address_3')    or '',
            'gender':       corr.get('gender')       or cbs.get('gender'),
        })

    if not merged_pay2:
        return {}
    cbs_rows2 = pd.DataFrame(merged_pay2)
    cbs_pay_lookup = cbs_rows2.set_index('account_number')

    missing_dict = {}
    for acc in add_accs:
        if acc not in cbs_pay_lookup.index:
            continue
        row = cbs_pay_lookup.loc[acc]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]

        needs_province     = is_empty(row.get('province')) or map_province(row.get('province')) == 'Unmatched'
        needs_district     = is_empty(row.get('district')) or map_district(row.get('district')) == 'Unmatched'
        needs_municipality = is_empty(row.get('municipality')) or map_local(row.get('municipality')) == 'Unmatched'
        needs_gender       = is_empty(row.get('gender'))

        if any([needs_province, needs_district, needs_municipality, needs_gender]):
            missing_dict[acc] = {
                'account_number': acc,
                'is_nepalpay': acc in np_payment_accs,
                'address_1': str(row.get('address_1') or ''),
                'address_3': str(row.get('address_3') or ''),
                'needs_province': needs_province,
                'needs_district': needs_district,
                'needs_municipality': needs_municipality,
                'needs_gender': needs_gender,
            }

    return missing_dict


def _run_pipeline(file_path, uid):
    """Background thread: runs the full data pipeline with progress tracking."""
    try:
        f_df, n_df, f_step2, n_step2, stats = perform_step1_and_2(file_path, uid, uid=uid)

        _set_progress(uid, 5, 'active', 'Validating geographical data completeness...')

        fp_missing = check_missing_records(f_step2, ['province', 'district', 'municipality', 'gender'], find_account_col(f_step2))
        np_missing = check_missing_records(n_step2, ['province', 'district', 'municipality', 'gender'], find_account_col(n_step2))

        has_missing = bool(fp_missing or np_missing)
        total_missing = len(fp_missing) + len(np_missing)

        result_info = {
            'unique_id': uid,
            'fonepay_count': len(f_df),
            'nepalpay_count': len(n_df),
        }

        if has_missing:
            _set_progress(uid, 5, 'action_required', f'Found {total_missing} records still missing geographical data', extra={**result_info, 'log': f'Unable to resolve Province/District/Municipality for {total_missing} accounts via CBS.'})
            return

        # Merchant data clean → now process MB + Connect IPS for sheet 11
        _set_progress(uid, 6, 'active', 'Enriching Mobile Banking & Connect IPS users via CBS...')
        from .processors_users import run_enrichment
        users_missing, _ = run_enrichment(uid, auto_seed=True)

        if users_missing:
            _set_progress(
                uid, 6, 'action_required',
                f'Found {len(users_missing)} user records missing CBS attributes',
                extra={**result_info,
                       'log': f'Need manual review for {len(users_missing)} MB/IPS rows (country_code/gender/DOB).',
                       'users_missing_count': len(users_missing)},
            )
            return

        # Step 7: Pre-enrich payment-detail accounts + check for missing CBS data
        _set_progress(uid, 7, 'active', 'Validating payment detail geo & gender data via CBS...')
        pay_missing = _check_payment_detail_missing(uid)

        if pay_missing:
            _set_progress(
                uid, 7, 'action_required',
                f'Found {len(pay_missing)} payment-detail accounts still missing CBS data',
                extra={**result_info,
                       'log': f'{len(pay_missing)} accounts need Province/District/Municipality/Gender. Proceed to skip nulls or resolve manually.'},
            )
            return

        _set_progress(uid, 8, 'active', 'Generating final regulatory report formats...')
        report = _generate_final_report(uid)
        result_info['step3_filename'] = report['step3_filename']
        _set_progress(uid, 8, 'complete', 'Report compiled successfully', extra={**result_info, 'log': 'Aggregated Province, District, Local Level, Gender and Sheet 11 Users.'})
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
    """Accept files, start background pipeline, return unique_id."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    file = request.FILES.get('file')
    card_data = request.FILES.get('card_data')
    phonepay_details = request.FILES.get('phonepay_details')
    nepalpay_details = request.FILES.get('nepalpay_details')
    cardless_report = request.FILES.get('cardless_report')
    mobile_banking = request.FILES.get('mobile_banking')
    connect_ips = request.FILES.get('connect_ips')

    required = [file, card_data, phonepay_details, nepalpay_details, cardless_report,
                mobile_banking, connect_ips]
    if not all(required):
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
    save_file(mobile_banking, 'mb_users')
    save_file(connect_ips, 'ips_users')

    # Save report meta (month, year) for use at finalize time.
    _save_meta(uid, {
        'month': request.POST.get('month', ''),
        'year':  request.POST.get('year', ''),
    })

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
    """Start final report generation in a background thread; caller polls api_progress for completion."""
    _set_progress(unique_id, 8, 'active', 'Generating final report...')
    def _run():
        try:
            report = _generate_final_report(unique_id)
            _set_progress(unique_id, 8, 'complete', 'Report compiled successfully', extra={
                'unique_id': unique_id,
                'step3_filename': report['step3_filename'],
            })
        except Exception as e:
            _set_progress(unique_id, -1, 'error', str(e))
        finally:
            connection.close()
    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({'status': 'pending'})


def api_classify_municipality(request):
    """
    POST { "accounts": [ {"account": "...", "address_1": "...", "address_3": "...",
                          "needs_province": true, "needs_district": true, "needs_municipality": true}, ... ] }
    Returns { "results": { "<account>": {"province": "Bagmati", "district": "Kathmandu District", "municipality": "MC"}, ... } }
    Classifies whichever fields are needed, saves results to CleanCBS for future runs.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    try:
        body = json.loads(request.body)
        accounts = body.get('accounts', [])
    except (json.JSONDecodeError, KeyError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    if not accounts:
        return JsonResponse({'results': {}})

    VALID_PROVINCES = set(provinces_list)
    VALID_DISTRICTS = set(districts_list)
    VALID_MUNI      = {'MP', 'MC', 'Sub MP', 'RM'}

    SYSTEM_PROMPT = (
        "You are a Nepal address classifier. For each account classify the requested fields using the place names in the address.\n\n"
        "Valid provinces: Koshi, Madhesh, Bagmati, Gandaki, Lumbini, Karnali, Sudurpaschim\n\n"
        "Valid districts — use exact spelling with ' District' suffix. All 77 Nepal districts are valid.\n\n"
        "Valid municipality types:\n"
        "  MP  = Metropolitan City (Kathmandu, Pokhara, Lalitpur, Bharatpur, Biratnagar, Birgunj, "
        "Dharan, Hetauda, Butwal, Siddharthanagar, Madhyapur Thimi, Mechinagar)\n"
        "  Sub MP = Sub-Metropolitan City (Dhankuta, Itahari, Damak, Birtamod, Urlabari, Bhadrapur, "
        "Inaruwa, Rajbiraj, Lahan, Janakpur, Malangwa, Kalaiya, Simara, Bharatpur already MP, "
        "Ratnanagar, Bhimdatt, Dhangadhi, Tulsipur, Ghorahi)\n"
        "  MC  = Municipality (any named town/bazaar that is a municipality — e.g. Tansen, Khairahani, "
        "Belkotgadhi, Bhimeshwor, Dhulikhel, Panauti, Banepa, Bidur, Trishuli, Damauli, Waling, "
        "Putalibazar, Baglung, Musikot, Liwang, Salyan, Surkhet, Dipayal, Tikapur, Lamki)\n"
        "  RM  = Rural Municipality (village, gaun, VDC-era names, rural areas)\n\n"
        "Rules:\n"
        "- Be aggressive — if you recognise the place name as a known Nepal settlement, classify it.\n"
        "- Only return null if the address text gives truly no usable location information.\n"
        "- Province mapping hints: Kathmandu/Lalitpur/Bhaktapur/Chitwan → Bagmati; "
        "Kaski/Pokhara → Gandaki; Jhapa/Morang/Sunsari → Koshi; "
        "Rupandehi/Kapilvastu/Palpa → Lumbini; Kailali/Kanchanpur → Sudurpaschim; "
        "Dhanusha/Sarlahi/Mahottari → Madhesh; Surkhet/Dailekh/Jumla → Karnali.\n\n"
        "Return ONLY a JSON object — no explanation, no markdown:\n"
        '{"<account>": {"province": "Bagmati", "district": "Kathmandu District", "municipality": "MC"}, ...}\n'
        "Include only the keys that were requested. Use null for genuinely unresolvable values."
    )

    def _classify_batch(batch, client):
        lines = []
        for item in batch:
            addr1 = str(item.get('address_1') or '').strip()[:60]
            addr3 = str(item.get('address_3') or '').strip()[:60]
            needs = []
            if item.get('needs_province'):     needs.append('province')
            if item.get('needs_district'):     needs.append('district')
            if item.get('needs_municipality'): needs.append('municipality')
            lines.append(f"{item['account']} [{','.join(needs)}]: {addr1}|{addr3}")
        user_msg = "Classify:\n" + '\n'.join(lines)

        def _call(max_tokens):
            resp = client.chat.completions.create(
                model='llama-3.1-8b-instant',
                max_completion_tokens=max_tokens,
                temperature=0,
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_msg},
                ]
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith('```'):
                raw = re.sub(r'^```[a-z]*\n?', '', raw)
                raw = re.sub(r'\n?```$', '', raw)
            return json.loads(raw)

        try:
            return _call(1200)
        except (json.JSONDecodeError, ValueError):
            # Retry with higher token budget in case response was cut off
            return _call(2000)

    try:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set in .env")

        client = Groq(api_key=api_key)
        all_results = {}

        # One API call per batch of 10 (keeps output well under token limit)
        batch_size = 10
        for i in range(0, len(accounts), batch_size):
            batch_results = _classify_batch(accounts[i:i + batch_size], client)
            all_results.update(batch_results)

        # Validate each field against allowed values
        clean = {}
        for acc, fields in all_results.items():
            if not isinstance(fields, dict):
                continue
            entry = {}
            prov = fields.get('province')
            dist = fields.get('district')
            muni = fields.get('municipality')
            if prov in VALID_PROVINCES:        entry['province']     = prov
            if dist in VALID_DISTRICTS:        entry['district']     = dist
            if muni in VALID_MUNI:             entry['municipality'] = muni
            clean[acc] = entry

        # Persist to CleanCBS — never overwrite an existing non-empty value
        for acc, fields in clean.items():
            if not fields:
                continue
            obj, _ = CleanCBS.objects.get_or_create(account_number=acc)
            changed = False
            for field in ('province', 'district', 'municipality'):
                val = fields.get(field)
                if val and not getattr(obj, field, None):
                    setattr(obj, field, val)
                    changed = True
            if changed:
                obj.save()

        return JsonResponse({'results': clean})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


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

    if not all_missing:
        return render(request, 'merchant/review_missing_data.html', {
            'unique_id': unique_id, 'missing_records': [],
            'provinces_list': provinces_list, 'districts_list': districts_list,
            'local_cats': local_cats, 'gender_choices': [('M', 'Male'), ('F', 'Female'), ('C', 'Company')],
        })

    all_accs = [d['account_number'] for d in all_missing]

    # Fresh lookup: CBS first, then CleanCBS corrections
    cbs_data = _cbs_source_lookup(all_accs)
    corrections = {}
    for chunk in [all_accs[i:i+900] for i in range(0, len(all_accs), 900)]:
        for r in CleanCBS.objects.filter(account_number__in=chunk).values(
            'account_number', 'province', 'district', 'municipality', 'address_1', 'address_3'
        ):
            corrections[r['account_number']] = r

    # For each missing field, try to resolve from CBS/CleanCBS right now.
    # Anything resolvable: patch step2 file + save to CleanCBS. Only show truly unresolvable.
    _mappers = {'province': map_province, 'district': map_district, 'municipality': map_local}
    f_patches, n_patches = {}, {}

    truly_missing = []
    for d in all_missing:
        acc = d['account_number']
        cbs  = cbs_data.get(acc, {})
        corr = corrections.get(acc, {})
        patches = f_patches if d['platform'] == 'FonePay' else n_patches

        still_needs = {'province': False, 'district': False, 'municipality': False}
        for field in ('province', 'district', 'municipality'):
            if not d.get(f'needs_{field}'):
                continue
            # CleanCBS first, CBS fallback
            raw_val = corr.get(field) or cbs.get(field) or ''
            resolved = _mappers[field](raw_val)
            if resolved != 'Unmatched' and raw_val:
                # Resolvable — patch step2 and CleanCBS
                if acc not in patches:
                    patches[acc] = {}
                patches[acc][field] = raw_val
            else:
                still_needs[field] = True

        # Only keep in review if at least one field is still unresolvable
        if any(still_needs.values()):
            d['needs_province']     = still_needs['province']
            d['needs_district']     = still_needs['district']
            d['needs_municipality'] = still_needs['municipality']
            truly_missing.append(d)

    # Patch step2 Excel files with auto-resolved values
    def _patch_df(df, patches):
        acc_col = find_account_col(df)
        if not acc_col or not patches:
            return df, False
        changed = False
        for idx, row in df.iterrows():
            acc = str(row.get(acc_col, '')).strip()
            if acc in patches:
                for col, val in patches[acc].items():
                    if col in df.columns and is_empty(row.get(col)):
                        df.at[idx, col] = val
                        changed = True
        return df, changed

    f_step2, f_changed = _patch_df(f_step2, f_patches)
    n_step2, n_changed = _patch_df(n_step2, n_patches)
    if f_changed: f_step2.to_excel(f_path2, index=False)
    if n_changed: n_step2.to_excel(n_path2, index=False)

    # Persist auto-resolved values to CleanCBS
    all_patches = {}
    for patches in (f_patches, n_patches):
        for acc, fields in patches.items():
            if acc not in all_patches:
                all_patches[acc] = {}
            all_patches[acc].update(fields)
    for acc, fields in all_patches.items():
        obj, _ = CleanCBS.objects.get_or_create(account_number=acc)
        changed = False
        for field, val in fields.items():
            if val and not getattr(obj, field, None):
                setattr(obj, field, val)
                changed = True
        if changed:
            obj.save()

    # Populate address + CBS presence for display
    for d in truly_missing:
        acc = d['account_number']
        r = cbs_data.get(acc) or corrections.get(acc, {})
        d['address_1']  = r.get('address_1') or ''
        d['address_3']  = r.get('address_3') or ''
        d['not_in_cbs'] = acc not in cbs_data

    return render(request, 'merchant/review_missing_data.html', {
        'unique_id': unique_id,
        'missing_records': truly_missing,
        'provinces_list': provinces_list,
        'districts_list': districts_list,
        'local_cats': local_cats,
        'gender_choices': [('M', 'Male'), ('F', 'Female'), ('C', 'Company')],
    })

def apply_manual_mapping(request, unique_id):
    if request.method == 'POST':
        f_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_fonepay_{unique_id}.xlsx')
        n_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_nepalpay_{unique_id}.xlsx')
        
        f_step2 = pd.read_excel(f_path2, dtype=str)
        n_step2 = pd.read_excel(n_path2, dtype=str)
        
        f_acc_col = find_account_col(f_step2)
        n_acc_col = find_account_col(n_step2)
        
        col_map = {'prov': 'province', 'dist': 'district', 'muni': 'municipality', 'gender': 'gender'}
        updates = {}
        for key, val in request.POST.items():
            if val and str(val).strip() and val != 'Ignore':
                parts = key.split('_', 1)
                if len(parts) == 2 and parts[0] in col_map:
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

        # Persist user fills into CleanCBS — never into the bank's CBSMerchant
        for acc, fields in updates.items():
            clean_obj, _ = CleanCBS.objects.get_or_create(account_number=acc)
            for col, val in fields.items():
                setattr(clean_obj, col, val)
            clean_obj.save()

        # Kick off final report generation in background then return to main page
        _set_progress(unique_id, 8, 'active', 'Generating final report...')
        def _run():
            try:
                report = _generate_final_report(unique_id)
                _set_progress(unique_id, 8, 'complete', 'Report compiled successfully', extra={
                    'unique_id': unique_id,
                    'step3_filename': report['step3_filename'],
                })
            except Exception as e:
                _set_progress(unique_id, -1, 'error', str(e))
            finally:
                connection.close()
        threading.Thread(target=_run, daemon=True).start()
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(f'/?resume={unique_id}')
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


# --- Sheet 11 Users Review (Option B: separate review page) ---

def users_review(request, unique_id):
    """Render manual-review form for MB/IPS rows whose CBS lookup was incomplete."""
    from .processors_users import load_rows, collect_missing
    from .country_codes import COUNTRY_CODE_MAP

    rows = load_rows(unique_id)
    if rows is None:
        raise Http404("Users data not found for this session.")

    missing = collect_missing(rows)
    if not missing:
        return redirect('finalize_report', unique_id=unique_id)

    country_choices = sorted(COUNTRY_CODE_MAP.items())
    return render(request, 'merchant/review_missing_users.html', {
        'unique_id': unique_id,
        'missing_records': missing,
        'country_choices': country_choices,
        'gender_choices': [('M', 'Male'), ('F', 'Female'), ('C', 'Company')],
    })


def users_apply(request, unique_id):
    """Accept user-filled values for MB/IPS missing records, persist, proceed."""
    if request.method != 'POST':
        return redirect('users_review', unique_id=unique_id)

    from .processors_users import load_rows, save_rows, collect_missing

    rows = load_rows(unique_id)
    if rows is None:
        raise Http404("Users data not found for this session.")

    # Inputs are keyed by row index: cc_<idx>, g_<idx>, dob_<idx>
    for key, raw in request.POST.items():
        if not raw:
            continue
        val = str(raw).strip()
        if not val:
            continue
        if '_' not in key:
            continue
        prefix, idx_str = key.split('_', 1)
        try:
            idx = int(idx_str)
        except ValueError:
            continue
        if idx < 0 or idx >= len(rows):
            continue
        r = rows[idx]
        if prefix == 'cc' and not r.get('country_code'):
            r['country_code'] = val
        elif prefix == 'g' and not r.get('gender'):
            r['gender'] = val
        elif prefix == 'dob' and not r.get('dob'):
            try:
                r['dob'] = datetime.strptime(val, '%Y-%m-%d').date()
            except ValueError:
                pass

    save_rows(unique_id, rows)

    still_missing = collect_missing(rows)
    if still_missing:
        messages.warning(request, f'{len(still_missing)} record(s) still incomplete — please fill all fields.')
        return redirect('users_review', unique_id=unique_id)

    messages.success(request, 'Manual user data applied. Generating final report...')
    # Kick the final report generation now that sheet-11 data is complete.
    _generate_final_report(unique_id)
    _set_progress(unique_id, 8, 'complete', 'Report compiled successfully', extra={
        'unique_id': unique_id,
        'step3_filename': f'Additional_Payment_Report_ASCII_{unique_id}.xlsx',
    })
    return redirect('finalize_report', unique_id=unique_id)


# --- Payment Detail Missing Data Review ---

def payment_detail_review(request, unique_id):
    """Show missing province/district/municipality/gender for payment-detail accounts."""
    missing = _check_payment_detail_missing(unique_id)
    if not missing:
        return redirect('finalize_report', unique_id=unique_id)

    missing_list = []
    for idx, (acc, info) in enumerate(missing.items()):
        missing_list.append({**info, 'idx': idx})

    return render(request, 'merchant/review_missing_payment.html', {
        'unique_id': unique_id,
        'missing_records': missing_list,
        'provinces_list': provinces_list,
        'districts_list': districts_list,
        'local_cats': local_cats,
        'gender_choices': [('M', 'Male'), ('F', 'Female'), ('C', 'Company')],
    })


def payment_detail_apply(request, unique_id):
    """Save user-entered values for payment-detail missing accounts into CBS, then finalize."""
    if request.method != 'POST':
        return redirect('payment_detail_review', unique_id=unique_id)

    # Re-compute missing so we know which accounts and which fields need filling
    missing = _check_payment_detail_missing(unique_id)
    missing_list = list(missing.items())

    updates = {}  # acc → {field: value}
    for key, raw in request.POST.items():
        if not raw or not str(raw).strip():
            continue
        val = str(raw).strip()
        # Keys are like: prov_<idx>, dist_<idx>, muni_<idx>, gender_<idx>
        for prefix, col in [('prov', 'province'), ('dist', 'district'), ('muni', 'municipality'), ('gender', 'gender')]:
            if key.startswith(f'{prefix}_'):
                try:
                    idx = int(key[len(prefix)+1:])
                except ValueError:
                    continue
                if idx < 0 or idx >= len(missing_list):
                    continue
                acc = missing_list[idx][0]
                updates.setdefault(acc, {})[col] = val

    # Write user fills to CleanCBS — never to the bank's CBSMerchant
    for acc, fields in updates.items():
        clean_obj, _ = CleanCBS.objects.get_or_create(account_number=acc)
        for col, val in fields.items():
            setattr(clean_obj, col, val)
        clean_obj.save()

    # Generate the final report now (skipped/empty gender → Company; skipped geo fields → excluded from geo totals)
    report = _generate_final_report(unique_id)
    _set_progress(unique_id, 8, 'complete', 'Report compiled successfully', extra={
        'unique_id': unique_id,
        'step3_filename': report['step3_filename'],
    })
    return redirect('finalize_report', unique_id=unique_id)