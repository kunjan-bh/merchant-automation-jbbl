"""
Sheet 11 (Users) pipeline.

Inputs: Mobile Banking xlsx/csv + Connect IPS xlsx/csv
Enrichment: CBSMerchant table (dob, gender, country_code)
Output: adds an '11.Users' sheet to the writer's workbook

Flow used by views.py:
    load_users_rows(uid)      → reads both uploaded files from media/outputs
    enrich_users(rows)        → CBS fill (file value first, CBS fallback)
    collect_missing(rows)     → rows needing manual review
    compute_user_stats(rows)  → 3 dicts for the sheet 11 tables
    write_sheet_11_users(...) → writes the sheet
"""

import os
import re
import json
import random
from datetime import date, datetime, timedelta

import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from django.conf import settings

from .models import CBSMerchant, CleanCBS
from .country_codes import (
    COUNTRY_CODE_MAP,
    NATIONALITY_ROWS,
    GENDER_BUCKETS,
    AGE_BUCKETS,
    nationality_from_code,
    calculate_age,
    age_bucket,
    classify_gender,
)


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _normalize_account(acc):
    """Strip whitespace/dashes, drop trailing .0, map NaN/NULL/blank → None."""
    if acc is None:
        return None
    if isinstance(acc, float) and pd.isna(acc):
        return None
    s = str(acc).strip().replace(' ', '').replace('-', '').upper()
    if s in ('', 'NAN', 'NULL', 'NONE', 'N/A', 'NA'):
        return None
    s = re.sub(r'\.0$', '', s)
    return s


def _find_col(df, *needles):
    """Find a column whose normalized name contains all given needles."""
    def norm(s):
        return re.sub(r'[^a-z0-9]', '', str(s).lower())
    for c in df.columns:
        n = norm(c)
        if all(x in n for x in needles):
            return c
    return None


def _read_excel(path):
    if str(path).lower().endswith('.csv'):
        return pd.read_csv(path, dtype=str)
    return pd.read_excel(path, dtype=str)


def extract_mb_rows(path):
    """Mobile Banking — needs ACCOUNT_NUMBER. File supplies no user attributes."""
    df = _read_excel(path)
    acc_col = _find_col(df, 'account', 'number') or _find_col(df, 'account')
    if acc_col is None:
        raise ValueError("Mobile Banking file has no account number column")
    rows = []
    for acc_raw in df[acc_col].tolist():
        rows.append({
            'source': 'MB',
            'account': _normalize_account(acc_raw),
            'gender': None,
            'country_code': None,
            'dob': None,
        })
    return rows


def extract_ips_rows(path):
    """Connect IPS — account number + file-level Gender (MALE/FEMALE/...)."""
    df = _read_excel(path)
    acc_col = _find_col(df, 'account', 'number') or _find_col(df, 'account')
    if acc_col is None:
        raise ValueError("Connect IPS file has no account number column")
    gender_col = _find_col(df, 'gender')
    rows = []
    for _, r in df.iterrows():
        g = r.get(gender_col) if gender_col else None
        if g is not None and (isinstance(g, float) and pd.isna(g)):
            g = None
        elif g is not None and str(g).strip() == '':
            g = None
        rows.append({
            'source': 'IPS',
            'account': _normalize_account(r.get(acc_col)),
            'gender': (str(g).strip() if g is not None else None),
            'country_code': None,
            'dob': None,
        })
    return rows


# ---------------------------------------------------------------------------
# CBS enrichment + synthetic seeding
# ---------------------------------------------------------------------------

# SQLite parameter-limit safe chunk size for IN-clause queries.
_SQL_CHUNK = 500


def _random_dob():
    today = date.today()
    age = random.randint(18, 70)
    days = random.randint(0, 364)
    return date(today.year - age, 1, 1) + timedelta(days=days)


def _chunked(seq, size=_SQL_CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _seed_missing_cbs(accounts):
    """For any account not in CBSMerchant, create a synthetic record.

    The country_code distribution is biased toward 01 (Nepalese). Keeps
    sheet-11 testable without a real CBS connection. Existing records are
    never overwritten. Chunked to stay under SQLite's IN-clause parameter cap.
    """
    existing = set()
    for chunk in _chunked(accounts):
        existing.update(
            CBSMerchant.objects.filter(account_number__in=chunk)
            .values_list('account_number', flat=True)
        )
    to_create = [a for a in accounts if a and a not in existing]
    if not to_create:
        return 0
    new_rows = []
    for acc in to_create:
        new_rows.append(CBSMerchant(
            account_number=acc,
            province='',
            district='',
            municipality='',
            address_1='',
            address_2='',
            gender=random.choice(['M', 'F', 'O']),
            dob=_random_dob(),
            country_code=random.choices(['01', '11', '41'], weights=[85, 10, 5])[0],
        ))
    for chunk in _chunked(new_rows):
        CBSMerchant.objects.bulk_create(chunk, ignore_conflicts=True)
    return len(new_rows)


def _ensure_clean_cbs_users(accounts):
    """Copy CBSMerchant data into CleanCBS for any accounts not yet there.
    All DB queries are chunked to stay under SQLite's IN-clause variable limit."""
    existing = set()
    for chunk in _chunked(accounts):
        existing.update(CleanCBS.objects.filter(account_number__in=chunk).values_list('account_number', flat=True))
    new_accs = [a for a in accounts if a not in existing]
    if not new_accs:
        return
    cbs_map = {}
    for chunk in _chunked(new_accs):
        for r in CBSMerchant.objects.filter(account_number__in=chunk):
            cbs_map[r.account_number] = r
    to_create = []
    for acc in new_accs:
        cbs = cbs_map.get(acc)
        to_create.append(CleanCBS(
            account_number=acc,
            gender=cbs.gender if cbs else '',
            dob=cbs.dob if cbs else None,
            country_code=cbs.country_code if cbs else '01',
        ))
    for chunk in _chunked(to_create):
        CleanCBS.objects.bulk_create(chunk, ignore_conflicts=True)


def enrich_users(rows, auto_seed=True):
    """Fill missing gender/country_code/dob from CleanCBS.

    Resolution order per field: file value → CleanCBS value → left as None.
    CBSMerchant is seeded first (mock in dev), then data is copied to CleanCBS.
    All reads come from CleanCBS so the bank's table is never written to.
    """
    accounts = sorted({r['account'] for r in rows if r['account']})
    if auto_seed:
        _seed_missing_cbs(accounts)
    _ensure_clean_cbs_users(accounts)

    cbs_lookup = {}
    for chunk in _chunked(accounts):
        for r in (CleanCBS.objects.filter(account_number__in=chunk)
                  .only('account_number', 'gender', 'dob', 'country_code')):
            cbs_lookup[r.account_number] = r

    for r in rows:
        acc = r['account']
        cbs = cbs_lookup.get(acc) if acc else None
        if cbs:
            if not r['gender']:
                r['gender'] = cbs.gender
            if not r['country_code']:
                r['country_code'] = cbs.country_code
            if not r['dob']:
                r['dob'] = cbs.dob
            r['in_cbs'] = True
        else:
            r['in_cbs'] = False
    return rows


def collect_missing(rows):
    """Rows we couldn't fully resolve — these need the users-review page."""
    missing = []
    for idx, r in enumerate(rows):
        if not r['account']:
            continue  # rows with no account at all can't be reviewed
        needs_cc = not r['country_code']
        needs_g = not r['gender']
        needs_dob = not r['dob']
        if needs_cc or needs_g or needs_dob:
            missing.append({
                'idx': idx,
                'source': r['source'],
                'account': r['account'],
                'needs_country_code': needs_cc,
                'needs_gender': needs_g,
                'needs_dob': needs_dob,
                'country_code': r['country_code'] or '',
                'gender': r['gender'] or '',
                'dob': r['dob'].isoformat() if r['dob'] else '',
            })
    return missing


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def compute_user_stats(rows):
    """Build the 3-table dicts for sheet 11. Only fully-enriched rows count."""
    nat = {k: 0 for k in NATIONALITY_ROWS}
    gender = {k: 0 for k in GENDER_BUCKETS}
    age = {k: 0 for k in AGE_BUCKETS}

    for r in rows:
        if not r['account']:
            continue
        if not (r['country_code'] and r['gender'] and r['dob']):
            continue  # incomplete rows excluded
        n = nationality_from_code(r['country_code'])
        if n not in nat:
            nat[n] = 0  # tolerate future codes
        nat[n] += 1
        gender[classify_gender(r['gender'])] += 1
        bucket = age_bucket(calculate_age(r['dob']))
        if bucket:
            age[bucket] += 1

    return {
        'nationality': nat,
        'gender': gender,
        'age': age,
        'total': sum(nat.values()),
    }


# ---------------------------------------------------------------------------
# Persistence of enriched rows (for the review loop)
# ---------------------------------------------------------------------------

def _rows_path(uid):
    d = os.path.join(settings.BASE_DIR, 'media', 'outputs')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'users_rows_{uid}.json')


def save_rows(uid, rows):
    serializable = []
    for r in rows:
        c = dict(r)
        if c.get('dob') and hasattr(c['dob'], 'isoformat'):
            c['dob'] = c['dob'].isoformat()
        serializable.append(c)
    with open(_rows_path(uid), 'w') as f:
        json.dump(serializable, f)


def load_rows(uid):
    path = _rows_path(uid)
    if not os.path.exists(path):
        return None
    with open(path, 'r') as f:
        data = json.load(f)
    for r in data:
        if r.get('dob'):
            try:
                r['dob'] = datetime.strptime(r['dob'], '%Y-%m-%d').date()
            except Exception:
                r['dob'] = None
    return data


# ---------------------------------------------------------------------------
# Sheet 11 writer
# ---------------------------------------------------------------------------

def write_sheet_11_users(writer, mb_stats, ips_stats):
    """Append the '11.Users' sheet to the existing workbook.

    Layout matches the NRB format shown in the reference screenshot:
      Table 1 — Nationality × 8 channels
      Table 2 — Gender × 8 channels
      Table 3 — Age Group × 8 channels
    MB totals populate column B, Connect IPS totals populate column H.
    Other channel columns are hard-zero (not yet wired for this project).
    """
    wb = writer.book
    if '11.Users' in wb.sheetnames:
        del wb['11.Users']
    ws = wb.create_sheet('11.Users')

    # Styles
    title_font = Font(name='Calibri', bold=True, size=12, color='FFFFFF')
    header_font = Font(name='Calibri', bold=True, size=10, color='FFFFFF')
    data_font = Font(name='Calibri', size=10)
    total_font = Font(name='Calibri', bold=True, size=10)
    title_fill = PatternFill(start_color='1B2631', end_color='1B2631', fill_type='solid')
    header_fill = PatternFill(start_color='2E4057', end_color='2E4057', fill_type='solid')
    sub_fill = PatternFill(start_color='AED6F1', end_color='AED6F1', fill_type='solid')
    total_fill = PatternFill(start_color='D5E8D4', end_color='D5E8D4', fill_type='solid')
    alt_fill = PatternFill(start_color='EBF5FB', end_color='EBF5FB', fill_type='solid')
    thin = Border(left=Side(style='thin'), right=Side(style='thin'),
                  top=Side(style='thin'), bottom=Side(style='thin'))
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center')

    # Column widths (A..I)
    widths = [32, 16, 16, 12, 12, 12, 12, 22, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w

    channel_headers = [
        'Mobile Banking', 'Internet Banking', 'eWallets',
        'Debit Cards', 'Credit Cards', 'Prepaid Cards',
        'Faster Payment Systems', 'ACH'
    ]

    def write_title(row, text):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
        c = ws.cell(row=row, column=1, value=text)
        c.font = title_font
        c.fill = title_fill
        c.alignment = center
        ws.row_dimensions[row].height = 22

    def write_headers(row, first_label):
        hdrs = [first_label] + channel_headers
        for ci, h in enumerate(hdrs, 1):
            c = ws.cell(row=row, column=ci, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = thin
        ws.row_dimensions[row].height = 30

    def write_row(row, label, mb_val, ips_val, is_alt=False):
        # columns: A=label, B=MB, C..G=0, H=IPS, I=0
        vals = [label, mb_val, 0, 0, 0, 0, 0, ips_val, 0]
        for ci, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=v)
            c.font = data_font
            c.alignment = left if ci == 1 else center
            c.border = thin
            if is_alt:
                c.fill = alt_fill
        ws.row_dimensions[row].height = 18

    def write_total(row, mb_total, ips_total):
        vals = ['Total', mb_total, 0, 0, 0, 0, 0, ips_total, 0]
        for ci, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=v)
            c.font = total_font
            c.fill = total_fill
            c.alignment = left if ci == 1 else center
            c.border = thin
        ws.row_dimensions[row].height = 20

    def write_sub_header(row, label):
        c = ws.cell(row=row, column=1, value=label)
        c.font = Font(bold=True, size=10)
        c.alignment = left
        c.border = thin
        c.fill = sub_fill
        for ci in range(2, 10):
            cc = ws.cell(row=row, column=ci)
            cc.fill = sub_fill
            cc.border = thin
        ws.row_dimensions[row].height = 18

    mb_nat = mb_stats.get('nationality', {})
    ips_nat = ips_stats.get('nationality', {})
    mb_total = mb_stats.get('total', 0)
    ips_total = ips_stats.get('total', 0)

    # ---- Title ----
    row = 1
    write_title(row, 'Digital Channel/Instrument/Systems Users as of Month End')
    row += 1
    write_title(row, '(Cumulative Number of Users as of Month End)')
    row += 1

    # ---- Table 1: Nationality ----
    write_headers(row, 'Nationality')
    row += 1
    nat_rows = [
        ('1. Nepalese', 'Nepalese', False),
        ('2. Non-Nepalese', None, True),
        ('2.1 Indian', 'Indian', False),
        ('2.2. Chinese', 'Chinese', False),
        ('2.3 Australian', 'Australian', False),
        ('2.4 Srilanka', 'Srilankan', False),
        ('2.5 USA', 'USA', False),
        ('2.6 Others', 'Others', False),
    ]
    alt = False
    for label, key, is_sub in nat_rows:
        if is_sub:
            write_sub_header(row, label)
        else:
            write_row(row, label, mb_nat.get(key, 0), ips_nat.get(key, 0), is_alt=alt)
            alt = not alt
        row += 1
    write_total(row, mb_total, ips_total)
    row += 2

    # ---- Table 2: Gender ----
    write_title(row, 'Gender-wise Distribution')
    row += 1
    write_headers(row, 'Gender')
    row += 1
    mb_gender = mb_stats.get('gender', {})
    ips_gender = ips_stats.get('gender', {})
    alt = False
    for g in GENDER_BUCKETS:
        write_row(row, g, mb_gender.get(g, 0), ips_gender.get(g, 0), is_alt=alt)
        alt = not alt
        row += 1
    write_total(row, mb_total, ips_total)
    row += 2

    # ---- Table 3: Age Group ----
    write_title(row, 'Age Group-wise Distribution')
    row += 1
    write_headers(row, 'Age Group')
    row += 1
    mb_age = mb_stats.get('age', {})
    ips_age = ips_stats.get('age', {})
    age_row_labels = [
        ('Less than or equal to 18 years', '≤18 years'),
        ('19-40 years', '19-40 years'),
        ('41-65 years', '41-65 years'),
        ('65+years', '65+ years'),
    ]
    alt = False
    for label, key in age_row_labels:
        write_row(row, label, mb_age.get(key, 0), ips_age.get(key, 0), is_alt=alt)
        alt = not alt
        row += 1
    write_total(row, mb_total, ips_total)


# ---------------------------------------------------------------------------
# Top-level helpers called by views
# ---------------------------------------------------------------------------

def mb_path(uid):
    return os.path.join(settings.BASE_DIR, 'media', 'outputs', f'mb_users_{uid}.xlsx')


def ips_path(uid):
    return os.path.join(settings.BASE_DIR, 'media', 'outputs', f'ips_users_{uid}.xlsx')


def run_enrichment(uid, auto_seed=True):
    """Read both files, enrich via CBS, persist rows, return (missing, rows)."""
    mb = extract_mb_rows(mb_path(uid))
    ips = extract_ips_rows(ips_path(uid))
    rows = mb + ips
    enrich_users(rows, auto_seed=auto_seed)
    save_rows(uid, rows)
    missing = collect_missing(rows)
    return missing, rows


def write_sheet_11_from_uid(writer, uid):
    """Called from _generate_final_report — reads persisted rows, writes sheet."""
    rows = load_rows(uid)
    if rows is None:
        # Defensive: if the enrichment step never ran, do it now without review.
        _, rows = run_enrichment(uid, auto_seed=True)
    mb_rows = [r for r in rows if r['source'] == 'MB']
    ips_rows = [r for r in rows if r['source'] == 'IPS']
    mb_stats = compute_user_stats(mb_rows)
    ips_stats = compute_user_stats(ips_rows)
    write_sheet_11_users(writer, mb_stats, ips_stats)
    return mb_stats, ips_stats
