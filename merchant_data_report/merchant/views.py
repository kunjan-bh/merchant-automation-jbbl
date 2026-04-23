import io
from django.http import HttpResponse, FileResponse, Http404, JsonResponse
from django.conf import settings
from django.urls import reverse
from django.db import connection
from django.shortcuts import render
import pandas as pd
import os
import uuid
import random
import json
import threading
import re
from datetime import date, datetime, timedelta
from django.utils import timezone
from django.shortcuts import redirect
from .models import CBSMerchant, CleanCBS, ReportBatch
from .cbs_source import cbs_lookup as _cbs_source_lookup, cbs_accounts_in_set, normalize_cbs_record
from .country_codes import calculate_age as _calculate_age
from .auth import login_required_custom
from .logger import log_generate_start, log_generate_success, log_generate_error, log_download

# --- HELPER FUNCTIONS ---

def is_empty(val):
    return pd.isna(val) or str(val).strip() == '' or str(val).lower() == 'null'

def is_valid_account_number(acc):
    """Valid account: all digits, exactly 20 characters. Rejects names, phone numbers, invalid length, etc."""
    if not acc:
        return False
    s = str(acc).strip()
    return s.isdigit() and len(s) == 20

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

def _is_valid_gender(v):
    """True only for canonical gender codes. Junk like CBS Cust IDs or branch
    codes leaking into NepalPay's gender column should be treated as missing."""
    if v is None: return False
    s = str(v).strip().upper()
    if not s or s in ('NULL', 'NAN', 'NONE'): return False
    return s in ('M', 'F', 'O', 'C')

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

        # Gender is graded separately — junk values (e.g. CBS Cust IDs leaking
        # in from a misaligned NepalPay export) flag the dropdown on rows
        # already being reviewed for other reasons. Don't promote a row into
        # review just for gender — that would balloon the review queue.
        gender_invalid = not _is_valid_gender(row.get('gender'))

        if missing or row.get('_invalid_format') == True:
            if acc not in missing_dict:
                missing_dict[acc] = {
                    'account_number': acc,
                    'address_1': '',  # overwritten from DB in review_missing_data
                    'address_3': '',  # overwritten from DB in review_missing_data
                    'needs_province': 'province' in missing,
                    'needs_district': 'district' in missing,
                    'needs_municipality': 'municipality' in missing,
                    'needs_gender': gender_invalid,
                    'needs_account_fix': row.get('_invalid_format') == True,
                    # Store current values even if they aren't "missing" so UI can show them/POST them back
                    'province': row.get('province') if not is_empty(row.get('province')) else '',
                    'district': row.get('district') if not is_empty(row.get('district')) else '',
                    'municipality': row.get('municipality') if not is_empty(row.get('municipality')) else '',
                    'gender': row.get('gender') if _is_valid_gender(row.get('gender')) else '',
                }
            else:
                for m in missing:
                    missing_dict[acc][f'needs_{m}'] = True
                if gender_invalid:
                    missing_dict[acc]['needs_gender'] = True
                if row.get('_invalid_format') == True:
                    missing_dict[acc]['needs_account_fix'] = True
                # Optional: update values if they were empty but now found (unlikely in this loop)
                for f in ('province', 'district', 'municipality'):
                    if not missing_dict[acc].get(f):
                        val = row.get(f)
                        if not is_empty(val): missing_dict[acc][f] = val
                if not missing_dict[acc].get('gender') and _is_valid_gender(row.get('gender')):
                    missing_dict[acc]['gender'] = row.get('gender')
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

# District → Province lookup: derive province from district without AI
_DISTRICT_TO_PROVINCE = {}
_koshi_districts = ['Bhojpur', 'Dhankuta', 'Ilam', 'Jhapa', 'Khotang', 'Morang', 'Okhaldhunga', 'Panchthar', 'Sankhuwasabha', 'Solukhumbu', 'Sunsari', 'Taplejung', 'Tehrathum', 'Udayapur']
_madhesh_districts = ['Bara', 'Parsa', 'Rautahat', 'Sarlahi', 'Dhanusha', 'Siraha', 'Mahottari', 'Saptari']
_bagmati_districts = ['Sindhuli', 'Ramechhap', 'Dolakha', 'Bhaktapur', 'Dhading', 'Kathmandu', 'Kavrepalanchok', 'Lalitpur', 'Nuwakot', 'Rasuwa', 'Sindhupalchok', 'Chitwan', 'Makwanpur']
_gandaki_districts = ['Baglung', 'Gorkha', 'Kaski', 'Lamjung', 'Manang', 'Mustang', 'Myagdi', 'Nawalpur', 'Parbat', 'Syangja', 'Tanahun']
_lumbini_districts = ['Arghakhanchi', 'Gulmi', 'Kapilvastu', 'Parasi', 'Palpa', 'Rupandehi', 'Banke', 'Bardiya', 'Dang', 'Pyuthan', 'Rolpa', 'Rukum East']
_karnali_districts = ['Dailekh', 'Dolpa', 'Humla', 'Jajarkot', 'Jumla', 'Kalikot', 'Mugu', 'Rukum West', 'Salyan', 'Surkhet']
_sudurpaschim_districts = ['Achham', 'Baitadi', 'Bajhang', 'Bajura', 'Dadeldhura', 'Darchula', 'Doti', 'Kailali', 'Kanchanpur']
for _d in _koshi_districts:       _DISTRICT_TO_PROVINCE[_d.lower()] = 'Koshi'
for _d in _madhesh_districts:     _DISTRICT_TO_PROVINCE[_d.lower()] = 'Madhesh'
for _d in _bagmati_districts:     _DISTRICT_TO_PROVINCE[_d.lower()] = 'Bagmati'
for _d in _gandaki_districts:     _DISTRICT_TO_PROVINCE[_d.lower()] = 'Gandaki'
for _d in _lumbini_districts:     _DISTRICT_TO_PROVINCE[_d.lower()] = 'Lumbini'
for _d in _karnali_districts:     _DISTRICT_TO_PROVINCE[_d.lower()] = 'Karnali'
for _d in _sudurpaschim_districts: _DISTRICT_TO_PROVINCE[_d.lower()] = 'Sudurpaschim'
# Also add alias spellings
for _alias, _canonical in _DISTRICT_ALIAS.items():
    _canon_lower = _canonical.lower()
    if _canon_lower in _DISTRICT_TO_PROVINCE:
        _DISTRICT_TO_PROVINCE[_alias] = _DISTRICT_TO_PROVINCE[_canon_lower]


def province_from_district(district_str):
    """Derive province from a known district name. Returns province or None."""
    if not district_str or pd.isna(district_str):
        return None
    d = str(district_str).strip().replace(' District', '').lower()
    return _DISTRICT_TO_PROVINCE.get(d)

# ---------------------------------------------------------------------------
# Address-based municipality type classifier (deterministic, no AI)
# Adapted from excel_files/add_municipality_type.py
# ---------------------------------------------------------------------------
_MP_CITIES = {"kathmandu", "lalitpur", "bharatpur", "pokhara", "biratnagar", "birgunj"}
_SUB_MP_CITIES = {"dharan", "itahari", "hetauda", "butwal", "siddharthanagar", "dhangadhi",
                  "tulsipur", "ghorahi", "janakpur", "kirtipur", "madhyapur thimi"}
_MC_TOWNS = {
    "inaruwa", "duhabi", "rajbiraj", "lahan", "siraha", "triyuga", "diktel rupakot majhuwagadhi",
    "phungling", "ilam", "birtamod", "damak", "mechinagar", "urlabari", "belbari", "rangeli",
    "sundar haraicha", "letang bhogateni", "budhabare", "kankai", "phidim", "taplejung", "khandbari",
    "chainpur", "bhojpur", "dhankuta", "pakhribas", "hile", "tehrathum", "myanglung", "solududhkunda",
    "salleri", "kalaiya", "gaur", "malangwa", "jaleshwar", "lalbandi", "bardibas", "mirchaiya",
    "hanumannagar kankalini", "golbazar", "kamala", "chandranigahapur", "garuda", "gadhimai",
    "simraungarh", "kolhabi", "ishworpur", "kariyamai", "pokhariya", "bindabasini", "dewahi gonahi",
    "baudihawa", "sursand", "pipra", "bidur", "trisuli", "belkotgadhi", "dupcheshwar", "suryagadhi",
    "kakani", "kageshwari manohara", "budhanilkantha", "gokarneshwor", "tokha", "tarakeshwor",
    "chandragiri", "dakshinkali", "konjyosom", "mahalaxmi", "godawari", "bagmati", "makwanpurgadhi",
    "thaha", "manahari", "raksirang", "kailash", "bhimphedi", "dhulikhel", "panauti", "panchkhal",
    "namobuddha", "charikot", "dolakha", "jiri", "gaurishankar", "ramechhap", "manthali", "doramba",
    "likhu tamakoshi", "sindhuli", "kamalamai", "sunkoshi", "tinpatan", "golanjor", "hariharpurgadhi",
    "dudhauli", "chautara sangachowk gadhi", "balephi", "helambu", "jugal", "bhotekoshi", "indrawati",
    "melamchi", "nuwakot", "tadi", "panchpokhari thangpaldhap", "waling", "putalibazar", "bhirkot",
    "arjunchaupari", "galyang", "harinas", "biruwa", "annapurna", "machhapuchchhre", "rupa", "madi",
    "beshishahar", "rainas", "sundarbazar", "dordi", "dudhpokhari", "marsyangdi", "gorkha", "palungtar",
    "arughat", "barpak sulikot", "tsum nubri", "siranchok", "ajirkot", "manang ngisyang", "mustang",
    "gharapjhong", "lomanthang", "thasang", "baglung", "dhorpatan", "bareng", "kanthekhola",
    "nisikhola", "jaimini", "burtibang", "myagde", "bandipur", "bhimad", "dulegaunda", "nawlpur",
    "aanbukhaireni", "rishing", "kushma", "phalewas", "modi", "painyu", "tansen", "rampur", "ribdikot",
    "nisdi", "tinau", "rambha", "mathagadhi", "devdaha", "saljhandi", "omsatiya", "sammarimai",
    "kanchan", "marchawari", "sunwal", "pratappur", "lamahi", "shantinagar", "rajpur", "babai",
    "rapti", "barbardiya", "bheriganga", "geruwa", "bansgadhi", "thakurbaba", "badhaiatal",
    "krishnanagar", "narainapur", "kohalpur", "raptisonari", "kapilvastu", "banganga", "maharajgunj",
    "shivaraj", "buddhabhumi", "yashodhara", "bijaynagar", "suddhodhan", "palhi nandan", "rohini",
    "pyuthan", "sarumarani", "mallarani", "naubahini", "mandavi", "jhimruk", "arghakhanchi",
    "sandhikharka", "panini", "bhumekasthan", "chhatradev", "jumla", "chandannath", "tatopani",
    "tila", "sinja", "kanakasundari", "hima", "dunai", "thuli bheri", "tripurasundari", "she phoksundo",
    "jagadulla", "mudkechula", "dolpo buddha", "simkot", "namkha", "soru", "chhayanath rara",
    "khatyad", "kalikot", "raskot", "tilagufa", "pachaljharana", "palata", "naraharinath",
    "sanni triveni", "dailekh", "dullu", "bhagawatimai", "dungeshwar", "chamunda bindrasaini",
    "gurans", "aathabis", "mahabu", "naumule", "bhairabi", "rukum east", "bhume", "sisne", "jajarkot",
    "barekot", "shivalaya", "nalgad", "chhedagad", "kushe", "birendranagar", "gurbhakot", "lekbeshi",
    "panchapuri", "bhimdatta", "shuklaphanta", "bedkot", "daijee", "punarbas", "mahakali",
    "lamkichuha", "ghodaghodi", "tikapur", "bhajani", "joshipur", "bardagoriya", "chure",
    "dipayal silgadhi", "bogtan fudsil", "purbichauki", "badikedar", "jorayal", "sayal", "shikhar",
    "mangalsen", "sanphebagar", "ramaroshan", "dhakari", "bannigadhi jayagadh", "mellekh",
    "chaurpati", "turmakhand", "jayaprithvi", "bungal", "talkot", "khaptad chhanna", "masta",
    "chhapiya", "durgathali", "kedarsyu", "saipal", "budhinanda", "tribeni", "himali", "badimalika",
    "budhiganga", "gaumul", "swamikartik khapar", "dasharathchand", "melauli", "patan (baitadi)",
    "purchaudi", "surnaya", "sigas", "dogadakedar", "dilasaini", "shailyashikhar", "naugad",
    "malikarjun", "apihimal", "duhun", "dunhu", "lekam", "marma",
}


def classify_municipality_from_address(addr1, addr3):
    """
    Classify municipality type from address text using keyword matching
    and official name lists. Returns 'MP'/'Sub MP'/'MC'/'RM' on positive
    match, or None if no confident classification could be made.

    Priority: addr3 first (usually has place name), then addr1.
    Only returns a value on POSITIVE match — never defaults to RM blindly.
    """
    def _classify_one(text):
        if not text or not isinstance(text, str):
            return None
        text_lower = text.lower().strip()
        if not text_lower:
            return None

        # Keyword matching (most reliable)
        if "rural municipality" in text_lower or "gaunpalika" in text_lower:
            return 'RM'
        if "sub-metropolitan" in text_lower or "sub metropolitan" in text_lower:
            return 'Sub MP'
        if "metropolitan" in text_lower or "metropolitian" in text_lower:
            return 'MP'
        if "municipality" in text_lower or "nagarpalika" in text_lower:
            return 'MC'

        # Name-list matching — exact match on normalized text
        norm = ' '.join(text_lower.replace("municipality", "").replace("rural", "")
                        .replace("metropolitan city", "").replace("sub metropolitan", "").split())
        if norm in _MP_CITIES:
            return 'MP'
        if norm in _SUB_MP_CITIES:
            return 'Sub MP'
        if norm in _MC_TOWNS:
            return 'MC'

        # Contains-match — address like "BARDIBAS-2,MAHOTTARI" or "BIRGUNJ MAISTHAN-12"
        # Check if any known city/town name appears in the text
        # MP/Sub MP lists are small and high-confidence, so safe to contains-match
        for city in _MP_CITIES:
            if city in text_lower:
                return 'MP'
        for city in _SUB_MP_CITIES:
            if city in text_lower:
                return 'Sub MP'
        # MC contains-match: only for names >= 4 chars to avoid false positives
        for town in _MC_TOWNS:
            if len(town) >= 4 and town in text_lower:
                return 'MC'

        return None  # no confident match — don't guess

    # Try addr3 first (usually has place/town name)
    result = _classify_one(str(addr3) if addr3 else '')
    if result:
        return result
    # Fallback to addr1
    return _classify_one(str(addr1) if addr1 else '')


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

    # Mark rows with invalid account numbers (names, phone numbers, etc.)
    # to be resolved in manual review instead of dropping them.
    if fonepay_acc_col and fonepay_acc_col in fonepay_df.columns:
        fonepay_df['_invalid_format'] = ~fonepay_df[fonepay_acc_col].apply(
            lambda x: is_valid_account_number(str(x).strip())
        )
        
    if nepalpay_acc_col and nepalpay_acc_col in nepalpay_df.columns:
        nepalpay_df['_invalid_format'] = ~nepalpay_df[nepalpay_acc_col].apply(
            lambda x: is_valid_account_number(str(x).strip())
        )
        
    if uid:
        invalid_rows = (fonepay_df['_invalid_format'].sum() if '_invalid_format' in fonepay_df.columns else 0) + \
                       (nepalpay_df['_invalid_format'].sum() if '_invalid_format' in nepalpay_df.columns else 0)
        
        # Calculate unique invalid accounts
        inv_accs = set()
        if fonepay_acc_col and '_invalid_format' in fonepay_df.columns:
            inv_accs.update(fonepay_df[fonepay_df['_invalid_format']][fonepay_acc_col].dropna().astype(str).tolist())
        if nepalpay_acc_col and '_invalid_format' in nepalpay_df.columns:
            inv_accs.update(nepalpay_df[nepalpay_df['_invalid_format']][nepalpay_acc_col].dropna().astype(str).tolist())
        invalid_acc_count = len(inv_accs)

        if invalid_rows > 0:
            _set_progress(uid, 1, 'active', f'Identified {invalid_rows} rows ({invalid_acc_count} unique accounts) with invalid format.', extra={'log': f'Found {invalid_rows} invalid rows across {invalid_acc_count} merchants.'})

    # Rebuild account sets after filtering — EXCLUDE invalid accounts
    if fonepay_acc_col:
        if '_invalid_format' in fonepay_df.columns:
            valid_fp = fonepay_df[~fonepay_df['_invalid_format']]
        else:
            valid_fp = fonepay_df
        fonepay_accs = set(valid_fp[fonepay_acc_col].dropna().astype(str).tolist())
    else:
        fonepay_accs = set()

    if nepalpay_acc_col:
        if '_invalid_format' in nepalpay_df.columns:
            valid_np = nepalpay_df[~nepalpay_df['_invalid_format']]
        else:
            valid_np = nepalpay_df
        nepalpay_accs = set(valid_np[nepalpay_acc_col].dropna().astype(str).tolist())
    else:
        nepalpay_accs = set()

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

    # Mark accounts from input file as confirmed merchants in CleanCBS
    _acc_list = [str(a) for a in all_accounts]
    for _chunk in [_acc_list[i:i+900] for i in range(0, len(_acc_list), 900)]:
        CleanCBS.objects.filter(account_number__in=_chunk, is_merchant__isnull=True).update(is_merchant=True)
        CleanCBS.objects.filter(account_number__in=_chunk, is_merchant=False).update(is_merchant=True)

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
    def _pick_val(cbs_dict, corr_dict, field):
        """CBS is source of truth. CleanCBS only fills gaps CBS doesn't have."""
        v = cbs_dict.get(field)
        if v and str(v).strip() and str(v).strip().lower() not in ('null', 'nan', ''):
            return str(v).strip() if isinstance(v, str) else v
        v = corr_dict.get(field)
        if v and str(v).strip() and str(v).strip().lower() not in ('null', 'nan', ''):
            return str(v).strip() if isinstance(v, str) else v
        return '' if field not in ('gender', 'dob') else None

    merged_rows = []
    for acc in all_accounts:
        cbs = raw_cbs_map.get(str(acc), {})
        corr = corrections_map.get(str(acc), {})
        prov = _pick_val(cbs, corr, 'province') or ''
        dist = _pick_val(cbs, corr, 'district') or ''
        # Derive province from district when province is empty or unmappable
        if dist and (not prov or map_province(prov) == 'Unmatched'):
            derived = province_from_district(dist)
            if derived:
                prov = derived
        merged_rows.append({
            'account_number': str(acc),
            'province':       prov,
            'district':       dist,
            'municipality':   _pick_val(cbs, corr, 'municipality') or '',
            'address_1':      _pick_val(cbs, corr, 'address_1') or '',
            'address_3':      _pick_val(cbs, corr, 'address_3') or '',
            'gender':         _pick_val(cbs, corr, 'gender'),
            'dob':            _pick_val(cbs, corr, 'dob'),
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

    # Propagate _invalid_format to step2 so check_missing_records can see it
    if '_invalid_format' in fonepay_df.columns:
        fonepay_step2_df['_invalid_format'] = fonepay_df['_invalid_format']
    if '_invalid_format' in nepalpay_df.columns:
        nepalpay_step2_df['_invalid_format'] = nepalpay_df['_invalid_format']

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
    
    # Count invalid formats precisely for UI reporting
    invalid_f_count = int(fonepay_df['_invalid_format'].sum() if '_invalid_format' in fonepay_df.columns else 0)
    invalid_n_count = int(nepalpay_df['_invalid_format'].sum() if '_invalid_format' in nepalpay_df.columns else 0)
    invalid_total = invalid_f_count + invalid_n_count

    if uid:
        stats_file = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'enrichment_stats_{uid}.json')
        stats['invalid_accounts'] = invalid_total
        with open(stats_file, 'w') as sf:
            json.dump(stats, sf)

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

    # FINAL CLEANUP: Drop any records that are still invalid or were removed
    # This acts as a safety net for Step 3.
    def final_scrub(df, acc_col):
        if not acc_col or df.empty: return df
        return df[df[acc_col].apply(lambda x: is_valid_account_number(str(x).strip()))].copy()

    f_step2 = final_scrub(f_step2, f_acc_col)
    n_step2 = final_scrub(n_step2, n_acc_col)

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

        # Also build a step2 lookup — step2 files have enriched geo data for the
        # same merchant accounts that appear in the payment transaction files.
        step2_lookup = {}
        for _s2 in [f_step2, n_step2]:
            _s2_acc = find_account_col(_s2)
            if _s2_acc and not _s2.empty:
                for _, _row in _s2.iterrows():
                    _a = str(_row.get(_s2_acc, '')).strip()
                    if _a and _a not in step2_lookup:
                        _prov = _row.get('province') or _row.get('Province') or ''
                        _dist = _row.get('district') or _row.get('District') or ''
                        _muni = _row.get('municipality') or _row.get('Municipality') or ''
                        _gend = _row.get('gender') or _row.get('Gender') or ''
                        if str(_prov).strip() and str(_prov).strip().lower() not in ('nan', 'null', ''):
                            step2_lookup[_a] = {
                                'province': str(_prov).strip(), 'district': str(_dist).strip(),
                                'municipality': str(_muni).strip(), 'gender': str(_gend).strip(),
                            }

        merged_pay = []
        for acc in add_accs:
            cbs  = raw_cbs_pay.get(str(acc), {})
            corr = corr_pay.get(str(acc), {})
            s2   = step2_lookup.get(str(acc), {})

            # Priority: CBS (source of truth) → step2 (enriched) → CleanCBS (corrections for empty fields only)
            def _pick(field):
                # CBS first — it's the source of truth
                v = cbs.get(field)
                if v and str(v).strip() and str(v).strip().lower() not in ('null', 'nan'):
                    return str(v).strip()
                # Step2 enriched data
                v = s2.get(field)
                if v and str(v).strip() and str(v).strip().lower() not in ('null', 'nan'):
                    return str(v).strip()
                # CleanCBS corrections — only for fields CBS/step2 don't have
                v = corr.get(field)
                if v and str(v).strip() and str(v).strip().lower() not in ('null', 'nan'):
                    return str(v).strip()
                return ''

            prov = _pick('province')
            dist = _pick('district')
            # Derive province from district when empty or unmappable
            if dist and (not prov or map_province(prov) == 'Unmatched'):
                derived = province_from_district(dist)
                if derived:
                    prov = derived

            merged_pay.append({
                'account_number': str(acc),
                'province':     prov,
                'district':     dist,
                'municipality': _pick('municipality'),
                'gender':       _pick('gender') or None,
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

    # Final Processing Summary for UI
    f_len1 = len(f_step1) if not f_step1.empty else len(f_step2)
    n_len1 = len(n_step1) if not n_step1.empty else len(n_step2)
    total_in = f_len1 + n_len1
    total_out = len(f_step2) + len(n_step2)
    
    # Read CBS enrichment stats
    merged_cbs = 0
    invalid_accs = 0
    en_stats_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'enrichment_stats_{unique_id}.json')
    if os.path.exists(en_stats_path):
        with open(en_stats_path, 'r') as sf:
            en_rd = json.load(sf)
            merged_cbs = en_rd.get('municipality', {}).get('filled', 0)
            invalid_accs = en_rd.get('invalid_accounts', 0)
    
    # Total resolved (present in final output)
    muni_filled_final = (f_step2['municipality'].notna().sum() if 'municipality' in f_step2.columns else 0) + \
                        (n_step2['municipality'].notna().sum() if 'municipality' in n_step2.columns else 0)
    
    # Script/AI Mapped: those resolved after CBS lookup
    script_mapped = max(0, muni_filled_final - merged_cbs)

    # We'll save a simplified stats object
    report_data = {
        'step3_filename': step3_filename,
        'fonepay_count': f_len1,
        'nepalpay_count': n_len1,
        'total_records': total_in,
        'deleted_count': total_in - total_out,
        'merged_cbs_count': int(merged_cbs),
        'script_mapped_count': int(script_mapped),
        'invalid_account_count': int(invalid_accs),
        'fonepay_provinces': get_prov_counts(f_step2),
        'fonepay_districts': get_dist_counts(f_step2),
        'nepalpay_provinces': get_prov_counts(n_step2),
        'nepalpay_districts': get_dist_counts(n_step2),
    }

    stats_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'stats_{unique_id}.json')
    with open(stats_path, 'w') as f:
        json.dump(report_data, f)

    return report_data


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

        def _pick2(field):
            v = cbs.get(field)
            if v and str(v).strip() and str(v).strip().lower() not in ('null', 'nan', ''):
                return str(v).strip()
            v = corr.get(field)
            if v and str(v).strip() and str(v).strip().lower() not in ('null', 'nan', ''):
                return str(v).strip()
            return ''

        merged_pay2.append({
            'account_number': acc,
            'province':     _pick2('province'),
            'district':     _pick2('district'),
            'municipality': _pick2('municipality'),
            'address_1':    _pick2('address_1'),
            'address_3':    _pick2('address_3'),
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

        # Gender: if empty/null or not F/M, default to 'company' — no manual review needed
        gender = row.get('gender') or 'company'
        if str(gender).upper() not in ('F', 'M'):
            gender = 'company'
        # No accounts go to manual review anymore — all have gender defaults

    return missing_dict


def _run_pipeline(file_path, uid):
    """Background thread: runs the full data pipeline with progress tracking."""
    try:
        f_df, n_df, f_step2, n_step2, stats = perform_step1_and_2(file_path, uid, uid=uid)

        _set_progress(uid, 5, 'active', 'Validating geographical data completeness...')

        fp_missing = check_missing_records(f_step2, ['province', 'district', 'municipality', 'gender'], find_account_col(f_step2))
        np_missing = check_missing_records(n_step2, ['province', 'district', 'municipality', 'gender'], find_account_col(n_step2))

        has_missing = bool(fp_missing or np_missing)
        
        # Breakdown missing for'precise' logging
        invalid_fmt = sum(1 for d in fp_missing.values() if d.get('needs_account_fix')) + \
                      sum(1 for d in np_missing.values() if d.get('needs_account_fix'))
        cbs_missing = len(fp_missing) + len(np_missing) - invalid_fmt

        result_info = {
            'unique_id': uid,
            'fonepay_count': len(f_df),
            'nepalpay_count': len(n_df),
        }

        # Premium log for AI/Script analysis
        _set_progress(uid, 5, 'active', 'Scanning records for geographical patterns...', extra={'log': 'Analyzing address fields against 753 Local Level names via deterministic scripts.'})

        if has_missing:
            log_parts = []
            if invalid_fmt > 0: log_parts.append(f"{invalid_fmt} Invalid Accounts")
            if cbs_missing > 0: log_parts.append(f"{cbs_missing} missing in CBS")
            log_desc = " & ".join(log_parts) if log_parts else "record issues"
            
            _set_progress(uid, 5, 'action_required', f'Found {log_desc}', extra={**result_info, 'log': f'Manual Review required: {invalid_fmt} with bad formats and {cbs_missing} records not found in CBS.'})
            return

        # Merchant data clean → now process MB + Connect IPS for sheet 11
        _set_progress(uid, 6, 'active', 'Enriching Mobile Banking & Connect IPS users via CBS...')
        from .processors_users import run_enrichment
        users_missing, _, user_stats = run_enrichment(uid, auto_seed=True)

        discard_msg = ''
        if user_stats.get('total_invalid', 0) > 0:
            discard_msg = f" (Discarded {user_stats['total_invalid']} invalid accounts: {user_stats.get('invalid_mb_count', 0)} MB, {user_stats.get('invalid_ips_count', 0)} IPS)"

        if users_missing:
            _set_progress(
                uid, 6, 'action_required',
                f'Found {len(users_missing)} user records missing CBS attributes',
                extra={**result_info,
                       'log': f'Need manual review for {len(users_missing)} MB/IPS rows (country_code/gender/DOB).{discard_msg}',
                       'users_missing_count': len(users_missing),
                       'invalid_users': user_stats.get('total_invalid', 0)},
            )
            return
        elif user_stats.get('total_invalid', 0) > 0:
            _set_progress(uid, 6, 'active', f'Mobile Banking & Connect IPS users processed. (Discarded {user_stats["total_invalid"]} invalid accounts)', extra={'log': discard_msg})

        # Step 7: Pre-enrich payment-detail accounts + check for missing CBS data
        _set_progress(uid, 7, 'active', 'Validating payment detail gender data via CBS...')
        pay_missing = _check_payment_detail_missing(uid)

        if pay_missing:
            _set_progress(
                uid, 7, 'action_required',
                f'Found {len(pay_missing)} payment-detail accounts still missing CBS data',
                extra={**result_info,
                       'log': f'{len(pay_missing)} accounts need Gender. Proceed to skip nulls or resolve manually.'},
            )
            return

        _set_progress(uid, 8, 'active', 'Generating final regulatory report formats...')
        report = _generate_final_report(uid)
        result_info['step3_filename'] = report['step3_filename']
        _set_progress(uid, 8, 'complete', 'Report compiled successfully', extra={**result_info, 'log': 'Aggregated Province, District, Local Level, Gender and Sheet 11 Users.'})
        _mark_batch_completed(uid, result_info)
    except Exception as e:
        _set_progress(uid, -1, 'error', str(e))
        _mark_batch_errored(uid, str(e))
    finally:
        connection.close()


def _mark_batch_completed(uid, result_info):
    """Mark the ReportBatch for this unique_id as completed and save stats."""
    try:
        batch = ReportBatch.objects.filter(unique_id=uid).first()
        if not batch:
            return
        batch.status                = 'completed'
        batch.has_errors            = False
        batch.processed_at          = timezone.now()
        batch.total_records         = int(result_info.get('total_records',         0) or 0)
        batch.fonepay_count         = int(result_info.get('fonepay_count',         0) or 0)
        batch.nepalpay_count        = int(result_info.get('nepalpay_count',        0) or 0)
        batch.invalid_account_count = int(result_info.get('invalid_account_count', 0) or 0)
        batch.merged_cbs_count      = int(result_info.get('merged_cbs_count',      0) or 0)
        batch.final_report_filename = result_info.get('step3_filename', '') or ''
        batch.save()

        # Log batch completion to activity
        ActivityLog.objects.create(
            username=batch.generated_by,
            full_name=batch.generated_by_name,
            level='INFO',
            action='GENERATE_SUCCESS',
            detail=f'Batch {uid} completed. Report: {batch.final_report_filename}',
            batch=batch
        )
    except Exception as e:
        print(f'[_mark_batch_completed] {e}')


def _mark_batch_errored(uid, err):
    try:
        batch = ReportBatch.objects.filter(unique_id=uid).first()
        if not batch:
            return
        batch.status        = 'error'
        batch.has_errors    = True
        batch.error_message = str(err)[:2000]
        batch.processed_at  = timezone.now()
        batch.save(update_fields=['status', 'has_errors', 'error_message', 'processed_at'])
    except Exception as e:
        print(f'[_mark_batch_errored] {e}')


def _finalize_and_mark(uid, step3_filename):
    """Load persisted stats for a finished pipeline run and flip the
    ReportBatch row to `completed`. Used by the manual-review apply / skip
    paths so batches don't sit on `processing` forever after the user leaves
    the review page."""
    try:
        stats_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'stats_{uid}.json')
        rd = {}
        if os.path.exists(stats_path):
            with open(stats_path, 'r') as f:
                rd = json.load(f)
        _mark_batch_completed(uid, {**rd, 'step3_filename': step3_filename})
    except Exception as e:
        print(f'[_finalize_and_mark] {e}')


# --- VIEWS ---

@login_required_custom
def upload_merchant_data(request):
    """Serves the upload page. Processing is handled via API."""
    return render(request, 'merchant/upload.html', {
        'user':             request.session.get('user'),
        'full_name':        request.session.get('full_name', ''),
        'institution_name': getattr(settings, 'INSTITUTION_NAME', ''),
        'institution_code': getattr(settings, 'INSTITUTION_CODE', ''),
    })


# --- API VIEWS ---

@login_required_custom
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

    month_str = request.POST.get('month', '')
    year_str  = request.POST.get('year',  '')

    # Save report meta (month, year) for use at finalize time.
    _save_meta(uid, {'month': month_str, 'year': year_str})

    # ── Create ReportBatch row so this generation shows in dashboards ──
    user      = request.session.get('user', '')
    full_name = request.session.get('full_name', '')
    try:
        year_int = int(year_str) if year_str else 0
    except (TypeError, ValueError):
        year_int = 0
    try:
        batch = ReportBatch.objects.create(
            generated_by      = user,
            generated_by_name = full_name,
            month             = month_str or '—',
            year              = year_int,
            unique_id         = uid,
            status            = 'processing',
            institution_name  = getattr(settings, 'INSTITUTION_NAME', 'Jyoti Bikas Bank Limited'),
            institution_code  = getattr(settings, 'INSTITUTION_CODE', '12060'),
            upload_filename         = file.name,
            card_data_filename      = card_data.name,
            phonepay_filename       = phonepay_details.name,
            nepalpay_filename       = nepalpay_details.name,
            cardless_filename       = cardless_report.name,
            mobile_banking_filename = mobile_banking.name,
            connect_ips_filename    = connect_ips.name,
        )
        log_generate_start(request, month_str, year_str, batch=batch)
    except Exception as e:
        batch = None
        print(f'[api_start] Could not create ReportBatch: {e}')

    _set_progress(uid, 0, 'started', 'Pipeline initiated',
                  extra={'batch_id': batch.pk if batch else None})

    t = threading.Thread(target=_run_pipeline, args=(tmp_path, uid), daemon=True)
    t.start()

    return JsonResponse({
        'unique_id': uid,
        'batch_id':  batch.pk if batch else None,
    })

@login_required_custom
def api_progress(request, unique_id):
    """Return current pipeline progress as JSON."""
    path = _progress_path(unique_id)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return JsonResponse(json.load(f))
    return JsonResponse({'step': 0, 'status': 'waiting', 'detail': 'Initializing...'})

@login_required_custom
def api_finalize(request, unique_id):
    """Resume pipeline from step 6 onwards; runs in background, caller polls for progress.

    Pass ?skip_review=1 to bypass steps 6/7 action_required prompts and generate directly
    (used by 'Cancel & Proceed Without Updates' on review pages).
    """
    skip_review = request.GET.get('skip_review') == '1'

    def _run():
        try:
            from .processors_users import run_enrichment, load_rows, collect_missing

            # Step 6: Users enrichment
            _set_progress(unique_id, 6, 'active', 'Enriching Mobile Banking & Connect IPS users via CBS...')
            existing_rows = load_rows(unique_id)
            if existing_rows is not None and not collect_missing(existing_rows):
                pass  # already resolved in a previous review pass
            else:
                users_missing, _, user_stats = run_enrichment(unique_id, auto_seed=True)

                discard_msg = ''
                if user_stats.get('total_invalid', 0) > 0:
                    discard_msg = f" (Discarded {user_stats['total_invalid']} invalid accounts: {user_stats.get('invalid_mb_count', 0)} MB, {user_stats.get('invalid_ips_count', 0)} IPS)"

                if users_missing and not skip_review:
                    _set_progress(unique_id, 6, 'action_required',
                        f'Found {len(users_missing)} user records missing CBS attributes',
                        extra={'log': f'Need manual review for {len(users_missing)} MB/IPS rows (country_code/gender/DOB).{discard_msg}',
                               'users_missing_count': len(users_missing),
                               'invalid_users': user_stats.get('total_invalid', 0)})
                    return
                elif user_stats.get('total_invalid', 0) > 0:
                    _set_progress(unique_id, 6, 'active', f'Mobile Banking & Connect IPS users processed. (Discarded {user_stats["total_invalid"]} invalid accounts)', extra={'log': discard_msg})

            # Step 7: Payment detail geo check
            _set_progress(unique_id, 7, 'active', 'Validating payment detail gender data via CBS...')
            pay_missing = _check_payment_detail_missing(unique_id)
            if pay_missing and not skip_review:
                _set_progress(unique_id, 7, 'action_required',
                    f'Found {len(pay_missing)} payment-detail accounts still missing CBS data',
                    extra={'log': f'{len(pay_missing)} accounts need Gender. Proceed to skip nulls or resolve manually.'})
                return

            # Step 8: Generate final report
            _set_progress(unique_id, 8, 'active', 'Generating final report...')
            report = _generate_final_report(unique_id)
            _set_progress(unique_id, 8, 'complete', 'Report compiled successfully', extra={
                'unique_id': unique_id,
                'step3_filename': report['step3_filename'],
            })
            _finalize_and_mark(unique_id, report['step3_filename'])
        except Exception as e:
            _set_progress(unique_id, -1, 'error', str(e))
            _mark_batch_errored(unique_id, str(e))
        finally:
            connection.close()
    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({'status': 'pending'})


@login_required_custom
def api_classify_municipality(request):
    """
    POST { "accounts": [ {"account": "...", "address_1": "...", "address_3": "...",
                          "needs_district": true, "needs_municipality": true}, ... ] }

    AI only classifies district and municipality. Province is ALWAYS derived from district.
    CBSMerchant values are never overridden.

    Layer 1: CBS lookup — take existing district/municipality from CBS.
    Layer 2: AI (Groq) — only for district/municipality still empty after layer 1.
    Layer 3: Derive province from district (never AI).
    Layer 4: Save to CleanCBS ONLY if all three (province + district + municipality) are present.
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

    VALID_DISTRICTS = set(districts_list)
    VALID_MUNI      = {'MP', 'MC', 'Sub MP', 'RM'}

    # ---- Layer 1: CBS lookup — get existing correct values ----
    acc_numbers = [item['account'] for item in accounts]
    cbs_data = _cbs_source_lookup(acc_numbers)

    resolved = {}   # acc → {district, municipality}   (province derived later)
    still_need_ai = []

    for item in accounts:
        acc = item['account']
        cbs = cbs_data.get(acc, {})

        dist = cbs.get('district') or ''
        muni = cbs.get('municipality') or ''

        # Validate through mappers
        dist_valid = dist and map_district(dist) != 'Unmatched'
        muni_valid = muni and map_local(muni) != 'Unmatched'

        entry = {}
        if dist_valid: entry['district'] = map_district(dist)
        if muni_valid: entry['municipality'] = map_local(muni)

        resolved[acc] = entry

    # ---- Layer 1.5: Address-based municipality classification (deterministic) ----
    # Uses keyword matching + official name lists from add_municipality_type.py
    # Runs before AI — fast and accurate for addresses containing explicit type keywords
    for item in accounts:
        acc = item['account']
        if resolved[acc].get('municipality'):
            continue  # already resolved from CBS
        if not item.get('needs_municipality'):
            continue
        addr1 = item.get('address_1', '')
        addr3 = item.get('address_3', '')
        # Also try CBS address data if input addresses are empty
        cbs = cbs_data.get(acc, {})
        if not addr1: addr1 = cbs.get('address_1', '')
        if not addr3: addr3 = cbs.get('address_3', '')
        muni_type = classify_municipality_from_address(addr1, addr3)
        if muni_type:
            resolved[acc]['municipality'] = muni_type

    # Build AI list — only accounts still missing fields after CBS + address classification
    still_need_ai = []
    for item in accounts:
        acc = item['account']
        ai_needs = []
        if not resolved[acc].get('district')     and item.get('needs_district'):     ai_needs.append('district')
        if not resolved[acc].get('municipality') and item.get('needs_municipality'): ai_needs.append('municipality')

        if ai_needs:
            cbs = cbs_data.get(acc, {})
            still_need_ai.append({
                'account': acc,
                'address_1': item.get('address_1', '') or cbs.get('address_1', ''),
                'address_3': item.get('address_3', '') or cbs.get('address_3', ''),
                'needs': ai_needs,
                'known_district': resolved[acc].get('district', ''),
            })

    # ---- Layer 2: AI classification — district and municipality ONLY ----
    if still_need_ai:
        SYSTEM_PROMPT = (
            "You classify Nepali bank addresses into district and municipality type.\n"
            "You will ONLY be asked for \"district\" and/or \"municipality\". NEVER return province.\n\n"

            "=== DISTRICT ===\n"
            "Return EXACTLY one from this list (include ' District' suffix):\n"
            "Bhojpur District, Dhankuta District, Ilam District, Jhapa District, Khotang District, "
            "Morang District, Okhaldhunga District, Panchthar District, Sankhuwasabha District, "
            "Solukhumbu District, Sunsari District, Taplejung District, Tehrathum District, "
            "Udayapur District, Bara District, Parsa District, Rautahat District, Sarlahi District, "
            "Dhanusha District, Siraha District, Mahottari District, Saptari District, "
            "Sindhuli District, Ramechhap District, Dolakha District, Bhaktapur District, "
            "Dhading District, Kathmandu District, Kavrepalanchok District, Lalitpur District, "
            "Nuwakot District, Rasuwa District, Sindhupalchok District, Chitwan District, "
            "Makwanpur District, Baglung District, Gorkha District, Kaski District, "
            "Lamjung District, Manang District, Mustang District, Myagdi District, "
            "Nawalpur District, Parbat District, Syangja District, Tanahun District, "
            "Arghakhanchi District, Gulmi District, Kapilvastu District, Parasi District, "
            "Palpa District, Rupandehi District, Banke District, Bardiya District, "
            "Dang District, Pyuthan District, Rolpa District, Rukum East District, "
            "Dailekh District, Dolpa District, Humla District, Jajarkot District, "
            "Jumla District, Kalikot District, Mugu District, Rukum West District, "
            "Salyan District, Surkhet District, Achham District, Baitadi District, "
            "Bajhang District, Bajura District, Dadeldhura District, Darchula District, "
            "Doti District, Kailali District, Kanchanpur District\n\n"

            "=== MUNICIPALITY TYPE ===\n"
            "There are exactly 4 types. You MUST pick one:\n\n"

            "MP (Metropolitan City) — ONLY these 6 cities in all of Nepal:\n"
            "  Kathmandu, Pokhara, Lalitpur, Bharatpur, Biratnagar, Birgunj\n"
            "  If address is NOT one of these 6 cities, it is NOT MP.\n\n"

            "Sub MP (Sub-Metropolitan City) — ONLY these 11 cities:\n"
            "  Dharan, Hetauda, Butwal, Siddharthanagar, Itahari, Damak,\n"
            "  Janakpur, Dhangadhi, Tulsipur, Ghorahi, Mechinagar\n"
            "  If address is NOT one of these 11 cities, it is NOT Sub MP.\n\n"

            "MC (Municipality / Nagarpalika) — an established urban municipality.\n"
            "  Address typically contains a recognized town name like: Bidur, Baglung, Tansen,\n"
            "  Banepa, Dhulikhel, Ilam, Ratnanagar, Kohalpur, Lahan, Rajbiraj, etc.\n"
            "  These are well-known market towns and district headquarters.\n\n"

            "RM (Rural Municipality / Gaunpalika) — everything else.\n"
            "  Villages, rural VDC names, gaun, small settlements, any place that is NOT\n"
            "  a recognized town. Examples: Dupcheshwor, Rautbeshi, Kakani, Galchi, Likhu,\n"
            "  Helambu, Jugal, Bakaiya, etc.\n"
            "  IMPORTANT: Most places in Nepal are RM. If you are unsure, choose RM.\n"
            "  A place name containing 'Rural Municipality' or 'Gaunpalika' is always RM.\n\n"

            "=== HOW TO DECIDE ===\n"
            "1. address_3 usually has the place/town/VDC name — use it to identify district and type\n"
            "2. address_1 has ward/tole/landmark — secondary clue\n"
            "3. For district: match the place name to the district it belongs to\n"
            "4. For municipality: check if the place is one of the 6 MP cities → MP.\n"
            "   Else check if it is one of the 11 Sub MP cities → Sub MP.\n"
            "   Else if it is a known town/bazaar/nagar → MC.\n"
            "   Else → RM (default, most common).\n"
            "5. If you cannot determine from the address, return null — do NOT guess.\n\n"

            "=== OUTPUT FORMAT ===\n"
            "Return ONLY a JSON object. No explanation, no markdown, no brackets in keys.\n"
            '{"0600010012345000001": {"district": "Siraha District", "municipality": "RM"}, '
            '"0600010067890000001": {"municipality": "MC"}}'
        )

        def _classify_batch(batch, client):
            lines = []
            for item in batch:
                addr1 = str(item.get('address_1') or '').strip()[:80]
                addr3 = str(item.get('address_3') or '').strip()[:80]
                needs_str = ', '.join(item['needs'])
                # Include known district if available — helps AI classify municipality
                known_dist = item.get('known_district') or ''
                ctx = f"address_1={addr1} | address_3={addr3}"
                if known_dist:
                    ctx += f" | district={known_dist}"
                lines.append(f"{item['account']} [needs: {needs_str}] {ctx}")
            user_msg = "Classify these accounts:\n" + '\n'.join(lines)

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
                return _call(2000)

        try:
            from groq import Groq
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                raise ValueError("GROQ_API_KEY is not set in .env")
            client = Groq(api_key=api_key)

            batch_size = 10
            for i in range(0, len(still_need_ai), batch_size):
                batch = still_need_ai[i:i + batch_size]
                try:
                    batch_results = _classify_batch(batch, client)
                except Exception:
                    continue  # skip failed batch, don't crash entire request

                for raw_acc, fields in batch_results.items():
                    if not isinstance(fields, dict):
                        continue
                    # Strip any bracket/suffix the AI may echo back
                    acc = re.sub(r'\s*\[.*\]\s*$', '', str(raw_acc)).strip()
                    if acc not in resolved:
                        continue

                    # Only accept fields that were REQUESTED and are still missing
                    requested = set()
                    for item in batch:
                        if item['account'] == acc:
                            requested = set(item['needs'])
                            break

                    raw_dist = fields.get('district')
                    raw_muni = fields.get('municipality')

                    # Map to canonical forms immediately to ensure validation passes
                    dist_mapped = map_district(raw_dist) if raw_dist else 'Unmatched'
                    muni_mapped = map_local(raw_muni) if raw_muni else 'Unmatched'

                    if 'district' in requested and dist_mapped != 'Unmatched' and not resolved[acc].get('district'):
                        resolved[acc]['district'] = dist_mapped
                    if 'municipality' in requested and muni_mapped != 'Unmatched' and not resolved[acc].get('municipality'):
                        resolved[acc]['municipality'] = muni_mapped

            if uid := request.GET.get('uid'):
                # JS already updates local status, but we could log here if needed
                pass

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    # ---- Layer 3: Derive province from district (never AI) ----
    for acc, entry in resolved.items():
        if entry.get('district'):
            derived = province_from_district(entry['district'])
            if derived:
                entry['province'] = derived

    # ---- Layer 4: Save to CleanCBS ONLY if all three are present (merged) ----
    for acc, fields in resolved.items():
        obj = CleanCBS.objects.filter(account_number=acc).first()
        
        # Prepare combined data to check for completeness
        # Start with what we just resolved
        combined = {
            'province': fields.get('province'),
            'district': fields.get('district'),
            'municipality': fields.get('municipality')
        }
        # Fill in from existing CleanCBS if missing in resolved
        if obj:
            for f in ('province', 'district', 'municipality'):
                if not combined.get(f):
                    combined[f] = getattr(obj, f, None)
        
        # STRICTOR RULE: Only save/update if the final result is complete
        if not (combined.get('province') and combined.get('district') and combined.get('municipality')):
            continue  # incomplete — don't save

        if not obj:
            obj = CleanCBS.objects.create(account_number=acc, is_merchant=True)
            print(f"[AI-Save] NEW CleanCBS created for {acc}")
        
        needs_save = False
        # Update missing fields from resolved data
        for field in ('province', 'district', 'municipality'):
            val = fields.get(field)
            current = getattr(obj, field, None)
            if val and (not current or str(current).strip() == ''):
                setattr(obj, field, val)
                needs_save = True
                print(f"[AI-Save] Updating {field} for {acc} -> {val}")

        # Fill gender/dob/address from CBS (don't overwrite existing)
        cbs = cbs_data.get(acc, {})
        for field in ('gender', 'dob', 'country_code', 'address_1', 'address_3'):
            cbs_val = cbs.get(field)
            current = getattr(obj, field, None)
            if cbs_val and (not current or str(current).strip() == ''):
                setattr(obj, field, cbs_val)
                needs_save = True

        if not obj.is_merchant:
            obj.is_merchant = True
            needs_save = True

        if needs_save:
            obj.save()
            print(f"[AI-Save] SUCCESSFULLY PERSISTED {acc} to CleanCBS")

    return JsonResponse({'results': resolved})


def _prepare_manual_review_data(unique_id):
    """Helper to gather records needing manual review and auto-patch resolvable ones."""
    f_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_fonepay_{unique_id}.xlsx')
    n_path2 = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'step2_nepalpay_{unique_id}.xlsx')

    if not os.path.exists(f_path2) or not os.path.exists(n_path2):
        return None, None, None

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
        return [], {}, {}

    all_accs = [d['account_number'] for d in all_missing]
    cbs_data = _cbs_source_lookup(all_accs)
    corrections = {}
    for chunk in [all_accs[i:i+900] for i in range(0, len(all_accs), 900)]:
        for r in CleanCBS.objects.filter(account_number__in=chunk).values(
            'account_number', 'province', 'district', 'municipality', 'address_1', 'address_3'
        ):
            corrections[r['account_number']] = r

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
            if not d.get(f'needs_{field}'): continue
            raw_val = corr.get(field) or cbs.get(field) or ''
            resolved = _mappers[field](raw_val)
            if resolved != 'Unmatched' and raw_val:
                if acc not in patches: patches[acc] = {}
                patches[acc][field] = raw_val
            else:
                still_needs[field] = True

        if still_needs.get('municipality'):
            addr1 = cbs.get('address_1') or corr.get('address_1') or d.get('address_1', '')
            addr3 = cbs.get('address_3') or corr.get('address_3') or d.get('address_3', '')
            muni_type = classify_municipality_from_address(addr1, addr3)
            if muni_type:
                if acc not in patches: patches[acc] = {}
                patches[acc]['municipality'] = muni_type
                still_needs['municipality'] = False

        if any(still_needs.values()):
            d['needs_province']     = still_needs['province']
            d['needs_district']     = still_needs['district']
            d['needs_municipality'] = still_needs['municipality']
            truly_missing.append(d)

    # Patch Step 2 files
    def _patch_df(df, patches):
        acc_col = find_account_col(df)
        if not acc_col or not patches: return df, False
        changed = False
        for idx, row in df.iterrows():
            acc = str(row.get(acc_col, '')).strip()
            if acc in patches:
                for col, val in patches[acc].items():
                    if col in df.columns and is_empty(row.get(col)):
                        df.at[idx, col] = val
                        changed = True
        return df, changed

    f_s2_new, f_c = _patch_df(f_step2, f_patches)
    n_s2_new, n_c = _patch_df(n_step2, n_patches)
    if f_c: f_s2_new.to_excel(f_path2, index=False)
    if n_c: n_s2_new.to_excel(n_path2, index=False)

    # Persist high-confidence automated patches
    all_p = {**f_patches, **n_patches}
    for acc, fields in all_p.items():
        obj = CleanCBS.objects.filter(account_number=acc).first()
        if obj:
            # Update only if it leads to a complete record or if already complete
            ch = False
            for f, v in fields.items():
                if v and not getattr(obj, f, None):
                    setattr(obj, f, v); ch = True
            
            # Auto-derive province if missing but district found
            if not obj.province and obj.district:
                der = province_from_district(obj.district)
                if der: obj.province = der; ch = True
            
            if not obj.is_merchant: obj.is_merchant = True; ch = True
            
            # STRICTOR RULE: Only save if complete
            if ch and obj.province and obj.district and obj.municipality:
                obj.save()
        else:
            # Create new only if complete
            new_v = dict(fields)
            if not new_v.get('province') and new_v.get('district'):
                der = province_from_district(new_v['district'])
                if der: new_v['province'] = der
            
            if new_v.get('province') and new_v.get('district') and new_v.get('municipality'):
                CleanCBS.objects.create(account_number=acc, is_merchant=True, **{
                    k: v for k, v in new_v.items() if k in ('province', 'district', 'municipality')
                })

    for d in truly_missing:
        acc = d['account_number']
        r = cbs_data.get(acc) or corrections.get(acc, {})
        d['address_1']  = r.get('address_1') or ''
        d['address_3']  = r.get('address_3') or ''
        d['not_in_cbs'] = acc not in cbs_data
        
        # Ensure latest values are present (including those just patched above)
        p = f_patches if d['platform'] == 'FonePay' else n_patches
        acc_p = p.get(acc, {})
        for field in ('province', 'district', 'municipality', 'gender'):
            if not d.get(field):
                d[field] = acc_p.get(field) or r.get(field) or ''
    
    auto_resolved_count = len(all_missing) - len(truly_missing)
    return truly_missing, cbs_data, corrections, auto_resolved_count

@login_required_custom
def review_missing_data(request, unique_id):
    truly_missing, _, _, auto_resolved = _prepare_manual_review_data(unique_id)
    if truly_missing is None:
        raise Http404("Processed Data Files not found.")

    if not truly_missing:
        return render(request, 'merchant/review_missing_data.html', {
            'unique_id': unique_id, 'missing_records': [],
            'auto_resolved_count': auto_resolved,
            'provinces_list': provinces_list, 'districts_list': districts_list,
            'local_cats': local_cats, 'gender_choices': [('M', 'Male'), ('F', 'Female'), ('C', 'Company')],
        })

    return render(request, 'merchant/review_missing_data.html', {
        'unique_id': unique_id,
        'missing_records': truly_missing,
        'auto_resolved_count': auto_resolved,
        'provinces_list': provinces_list,
        'districts_list': districts_list,
        'local_cats': local_cats,
        'gender_choices': [('M', 'Male'), ('F', 'Female'), ('C', 'Company')],
    })

@login_required_custom
def download_review_list(request, unique_id):
    """Generates an Excel list of all accounts currently awaiting manual review."""
    truly_missing, _, _, _ = _prepare_manual_review_data(unique_id)
    if truly_missing is None:
        raise Http404("Processed Data Files not found.")

    export_rows = []
    for d in truly_missing:
        # Determine Condition
        if d.get('needs_account_fix'):
            cond = "Invalid Account"
        elif d.get('not_in_cbs'):
            cond = "Not in CBS"
        else:
            # Check which fields are still missing
            missing = []
            if d.get('needs_province'): missing.append("Province")
            if d.get('needs_district'): missing.append("District")
            if d.get('needs_municipality'): missing.append("Municipality")
            if d.get('needs_gender'): missing.append("Gender")
            cond = "Missing: " + ", ".join(missing) if missing else "Review Required"

        export_rows.append({
            'Account Number': d['account_number'],
            'Platform': d['platform'],
            'Condition': cond,
            'Address 1 (from CBS)': d.get('address_1', ''),
            'Address 3 (from CBS)': d.get('address_3', ''),
        })

    df = pd.DataFrame(export_rows)
    
    # Generate Excel in memory
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Manual Review List')
        
        # Simple auto-adjust columns
        worksheet = writer.sheets['Manual Review List']
        for i, col in enumerate(df.columns):
            max_len = max(df[col].astype(str).map(len).max(), len(col)) + 2
            worksheet.column_dimensions[chr(65+i)].width = min(max_len, 50)

    output.seek(0)
    
    filename = f"manual_review_list_{unique_id}.xlsx"
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename={filename}'
    return response

@login_required_custom
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
        account_fixes = {}  # old_acc -> new_acc
        to_delete = set()

        for key, val in request.POST.items():
            if not val or str(val).strip() == '':
                continue
            
            # 1. Handle Account Fixes
            if key.startswith('new_account_'):
                old_acc = key.replace('new_account_', '')
                new_acc = str(val).strip()
                if new_acc != old_acc:
                    account_fixes[old_acc] = new_acc
            
            # 2. Handle Deletions
            elif key.startswith('delete_') and val == 'true':
                acc = key.replace('delete_', '')
                to_delete.add(acc)

            # 3. Handle Geo/Gender Updates
            elif val != 'Ignore':
                parts = key.split('_', 1)
                if len(parts) == 2 and parts[0] in col_map:
                    col = col_map[parts[0]]
                    acc = parts[1]
                    if acc not in updates: updates[acc] = {}
                    updates[acc][col] = str(val).strip()
                    
        def patch_df(df, acc_col):
            if not acc_col or df.empty: return df
            rows_to_keep = []
            for idx, r in df.iterrows():
                acc = str(r.get(acc_col)).strip()
                
                # Check for deletions
                if acc in to_delete:
                    continue
                
                # Check for account fixes
                if acc in account_fixes:
                    df.at[idx, acc_col] = account_fixes[acc]
                    acc = account_fixes[acc] # update acc for subsequent updates
                
                # Check for geo updates
                if acc in updates:
                    for col, new_val in updates[acc].items():
                        if col in df.columns and is_empty(r.get(col)):
                            df.at[idx, col] = new_val
                
                rows_to_keep.append(idx)
            return df.loc[rows_to_keep].copy()
            
        f_step2 = patch_df(f_step2, f_acc_col)
        n_step2 = patch_df(n_step2, n_acc_col)

        f_step2.to_excel(f_path2, index=False)
        n_step2.to_excel(n_path2, index=False)

        # Persist user fills into CleanCBS — never into the bank's CBSMerchant
        for acc, fields in updates.items():
            final_acc = account_fixes.get(acc, acc)
            if final_acc in to_delete: continue

            clean_obj = CleanCBS.objects.filter(account_number=final_acc).first()
            
            # Prepare data set (merge existing + new)
            full_data = {}
            if clean_obj:
                full_data = {'province': clean_obj.province, 'district': clean_obj.district, 'municipality': clean_obj.municipality, 'gender': clean_obj.gender}
            
            # Override with new user inputs
            for col, val in fields.items():
                if val != 'Ignore':
                    full_data[col] = val

            # Auto-derive province if missing but district given
            if not full_data.get('province') and full_data.get('district'):
                derived = province_from_district(full_data['district'])
                if derived:
                    full_data['province'] = derived
            
            # STRICTOR RULE: Only save/update if the result is complete
            if full_data.get('province') and full_data.get('district') and full_data.get('municipality'):
                if clean_obj:
                    for k, v in full_data.items():
                        setattr(clean_obj, k, v)
                    clean_obj.is_merchant = True
                    clean_obj.save()
                else:
                    CleanCBS.objects.create(account_number=final_acc, is_merchant=True, **{
                        k: v for k, v in full_data.items()
                        if k in ('province', 'district', 'municipality', 'gender')
                    })
            # Else: We do NOT delete the existing record anymore. We just don't update it to CleanCBS 
            # if it's still missing pieces. This prevents loss of work (like gender fills).

        # Mappings saved — return to dashboard modal to continue steps 6, 7, 8
        return redirect(f'/dashboard/?resume={unique_id}&done=5')

@login_required_custom
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

    # Read persisted stats if available
    stats_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', f'stats_{unique_id}.json')
    if os.path.exists(stats_path):
        with open(stats_path, 'r') as f:
            rd = json.load(f)
    else:
        rd = {}

    # Mark the ReportBatch completed (if not already) so it appears in dashboards.
    batch = ReportBatch.objects.filter(unique_id=unique_id).first()
    if batch and batch.status != 'completed':
        try:
            _mark_batch_completed(unique_id, {**rd, 'step3_filename': step3_filename})
            batch.refresh_from_db()
            log_generate_success(request, batch)
        except Exception as e:
            print(f'[finalize_report] batch update failed: {e}')

    context = {
        'success': True,
        'unique_id': unique_id,
        'step3_filename': step3_filename,
        'batch_id': batch.pk if batch else None,
        'user':             request.session.get('user'),
        'full_name':        request.session.get('full_name', ''),
        'institution_name': getattr(settings, 'INSTITUTION_NAME', ''),
        'institution_code': getattr(settings, 'INSTITUTION_CODE', ''),
        'fonepay_count': rd.get('fonepay_count', 0),
        'nepalpay_count': rd.get('nepalpay_count', 0),
        'total_records': rd.get('total_records', 0),
        'deleted_count': rd.get('deleted_count', 0),
        'merged_cbs_count': rd.get('merged_cbs_count', 0),
        'script_mapped_count': rd.get('script_mapped_count', 0),
        'invalid_account_count': rd.get('invalid_account_count', 0),
        'fonepay_provinces': rd.get('fonepay_provinces', {}),
        'fonepay_districts': rd.get('fonepay_districts', {}),
        'nepalpay_provinces': rd.get('nepalpay_provinces', {}),
        'nepalpay_districts': rd.get('nepalpay_districts', {}),
        # Filenames for links
        'fonepay_filename': f'step1_fonepay_{unique_id}.xlsx',
        'nepalpay_filename': f'step1_nepalpay_{unique_id}.xlsx',
        'fonepay_step2_filename': f'step2_fonepay_{unique_id}.xlsx',
        'nepalpay_step2_filename': f'step2_nepalpay_{unique_id}.xlsx',
    }
    return render(request, 'merchant/upload.html', context)

@login_required_custom
def download_sheet(request, filename):
    file_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', filename)
    if os.path.exists(file_path):
        return FileResponse(open(file_path, 'rb'), as_attachment=True, filename=filename)
    else:
        raise Http404("File not found")


# --- Sheet 11 Users Review (Option B: separate review page) ---

@login_required_custom
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


@login_required_custom
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

    # User data saved — return to dashboard modal to continue steps 7 and 8
    return redirect(f'/dashboard/?resume={unique_id}&done=6')


# --- Payment Detail Missing Data Review ---

@login_required_custom
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


@login_required_custom
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
    # Only save if record will be complete (province + district + municipality)
    for acc, fields in updates.items():
        clean_obj = CleanCBS.objects.filter(account_number=acc).first()
        if clean_obj:
            for col, val in fields.items():
                setattr(clean_obj, col, val)
            if not clean_obj.province and clean_obj.district:
                derived = province_from_district(clean_obj.district)
                if derived:
                    clean_obj.province = derived
            if clean_obj.province and clean_obj.district and clean_obj.municipality:
                clean_obj.save()
            else:
                clean_obj.delete()
        else:
            if not fields.get('province') and fields.get('district'):
                derived = province_from_district(fields['district'])
                if derived:
                    fields['province'] = derived
            if fields.get('province') and fields.get('district') and fields.get('municipality'):
                CleanCBS.objects.create(account_number=acc, is_merchant=True, **{
                    k: v for k, v in fields.items()
                    if k in ('province', 'district', 'municipality')
                })

    # Payment data saved — return to dashboard modal to generate final report (step 8)
    return redirect(f'/dashboard/?resume={unique_id}&done=7')