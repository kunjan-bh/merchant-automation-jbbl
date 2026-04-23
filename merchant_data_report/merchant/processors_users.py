"""
Sheet 11 (Users) pipeline.

Inputs: Mobile Banking xlsx/csv + Connect IPS xlsx/csv
Enrichment: CBS data source (dob, gender, country_code) via cbs_source.cbs_lookup()
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
from openpyxl.styles import Font, Alignment, Border, Side
from django.conf import settings

from .models import CleanCBS
from .cbs_source import cbs_lookup
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
    """Mobile Banking — needs ACCOUNT_NUMBER. File supplies no user attributes.
    Filters out invalid account numbers (not 19-20 digits).
    """
    from .views import is_valid_account_number

    df = _read_excel(path)
    acc_col = _find_col(df, 'account', 'number') or _find_col(df, 'account')
    if acc_col is None:
        raise ValueError("Mobile Banking file has no account number column")
    rows = []
    invalid_count = 0
    for acc_raw in df[acc_col].tolist():
        normalized_acc = _normalize_account(acc_raw)
        if not normalized_acc or not is_valid_account_number(normalized_acc):
            invalid_count += 1
            continue  # Skip invalid or empty accounts
        rows.append({
            'source': 'MB',
            'account': normalized_acc,
            'gender': None,
            'country_code': None,
            'dob': None,
            '_invalid_format': False,
        })
    if invalid_count > 0:
        print(f'[MB] Discarded {invalid_count} rows with invalid account numbers')
    return rows, invalid_count


def extract_ips_rows(path):
    """Connect IPS — account number + file-level Gender (MALE/FEMALE/...).
    Filters out invalid account numbers (not 19-20 digits).
    """
    from .views import is_valid_account_number

    df = _read_excel(path)
    acc_col = _find_col(df, 'account', 'number') or _find_col(df, 'account')
    if acc_col is None:
        raise ValueError("Connect IPS file has no account number column")
    gender_col = _find_col(df, 'gender')
    rows = []
    invalid_count = 0
    for _, r in df.iterrows():
        normalized_acc = _normalize_account(r.get(acc_col))
        if not normalized_acc or not is_valid_account_number(normalized_acc):
            invalid_count += 1
            continue  # Skip invalid or empty accounts
        g = r.get(gender_col) if gender_col else None
        if g is not None and (isinstance(g, float) and pd.isna(g)):
            g = None
        elif g is not None and str(g).strip() == '':
            g = None
        rows.append({
            'source': 'IPS',
            'account': normalized_acc,
            'gender': (str(g).strip() if g is not None else None),
            'country_code': None,
            'dob': None,
            '_invalid_format': False,
        })
    if invalid_count > 0:
        print(f'[IPS] Discarded {invalid_count} rows with invalid account numbers')
    return rows, invalid_count


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
    """
    No-op. CBSMerchant is populated exclusively from the real CBS export
    (load_cbs_excel management command). Synthetic records are never injected.
    Accounts absent from CBS are flagged for manual input at review time.
    """
    return 0


def _ensure_clean_cbs_users(accounts):
    """
    Sync CBS data into CleanCBS for the given accounts.
    Only accounts known to CBS get a CleanCBS record — no empty shells.
    Uses cbs_lookup() so this is ready for the API swap.
    """
    accs = [a for a in accounts if a and str(a).strip()]
    if not accs:
        return

    existing = set()
    for chunk in _chunked(accs):
        existing.update(CleanCBS.objects.filter(account_number__in=chunk).values_list('account_number', flat=True))

    new_accs = [a for a in accs if a not in existing]
    if not new_accs:
        return

    # Single CBS source call — swap cbs_lookup() for API when ready
    cbs_map = cbs_lookup(new_accs)

    to_create = []
    for acc in new_accs:
        r = cbs_map.get(acc)
        if not r:
            continue   # not in CBS → skip, will appear in manual review
        to_create.append(CleanCBS(
            account_number=acc,
            gender=r['gender'] or '',
            dob=r['dob'],
            country_code=r['country_code'],
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


def apply_null_defaults(rows):
    """Apply defaults for null/invalid enrichment fields:
    - gender: if not 'F' or 'M', set to 'company'
    - country_code: if empty/null, set to '01'
    - dob: if empty/null, set to ~18 years ago (under 18 bracket)
    """
    from datetime import date, timedelta
    today = date.today()
    under_18_cutoff = today - timedelta(days=18*365.25)

    for r in rows:
        if not r['account']:
            continue
        # Gender: only accept F or M, else company
        if not r['gender'] or r['gender'].upper() not in ('F', 'M'):
            r['gender'] = 'company'
        if not r['country_code']:
            r['country_code'] = '01'
        if not r['dob']:
            r['dob'] = under_18_cutoff
    return rows


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

    Styled identically to Sheets 3-10: bold headers, thin borders, no fills/colours.

    Layout:
      Title row (merged) + sub-title row (merged)
      Table 1 — Nationality × 8 channels
      Table 2 — Gender × 8 channels
      Table 3 — Age Group × 8 channels
    MB totals populate column B, Connect IPS totals populate column H.
    Other channel columns are hard-zero.
    """
    from openpyxl.styles import Font, Alignment, Border, Side

    wb = writer.book
    if '11.Users' in wb.sheetnames:
        del wb['11.Users']
    ws = wb.create_sheet('11.Users')

    # --- Same styles used by Sheets 3-10 in views.py ---
    bold_font  = Font(bold=True)
    plain_font = Font()
    thin = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'),  bottom=Side(style='thin'),
    )
    center = Alignment(horizontal='center')
    left   = Alignment(horizontal='left')

    NUM_COLS = 9   # label + 8 channel columns

    channel_headers = [
        'Mobile Banking', 'Internet banking', 'eWallets',
        'Debit Cards', 'Credit Cards', 'Prepaid Cards',
        'Faster Payment Systems', 'ACH',
    ]

    # ------------------------------------------------------------------ helpers

    def _write_title(row, text):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=NUM_COLS)
        c = ws.cell(row=row, column=1, value=text)
        c.font   = bold_font
        c.alignment = center

    def _write_header_row(row, first_label):
        for ci, h in enumerate([first_label] + channel_headers, 1):
            c = ws.cell(row=row, column=ci, value=h)
            c.font   = bold_font
            c.border = thin
            c.alignment = center

    def _write_data_row(row, label, mb_val, ips_val):
        vals = [label, mb_val, 0, 0, 0, 0, 0, ips_val, 0]
        for ci, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=v)
            c.font   = plain_font
            c.border = thin
            c.alignment = left if ci == 1 else center

    def _write_total_row(row, mb_total, ips_total):
        vals = ['Total', mb_total, 0, 0, 0, 0, 0, ips_total, 0]
        for ci, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=v)
            c.font   = bold_font
            c.border = thin
            c.alignment = left if ci == 1 else center

    def _autofit(header_row, last_data_row):
        for ci in range(1, NUM_COLS + 1):
            max_len = 0
            for ri in range(header_row, last_data_row + 1):
                val = ws.cell(row=ri, column=ci).value
                if val is not None:
                    max_len = max(max_len, len(str(val)))
            letter = ws.cell(row=header_row, column=ci).column_letter
            ws.column_dimensions[letter].width = max(max_len + 2, 10)

    # ------------------------------------------------------------------ data

    mb_nat    = mb_stats.get('nationality', {})
    ips_nat   = ips_stats.get('nationality', {})
    mb_total  = mb_stats.get('total', 0)
    ips_total = ips_stats.get('total', 0)
    mb_gender = mb_stats.get('gender', {})
    ips_gender = ips_stats.get('gender', {})
    mb_age    = mb_stats.get('age', {})
    ips_age   = ips_stats.get('age', {})

    # ------------------------------------------------------------------ layout

    row = 1
    _write_title(row, 'Digital Channel/Instrument/ Systems Users as of Month End')
    row += 1
    _write_title(row, '(Cumulative Number of Users as of Month End)')
    row += 1

    # Table 1 — Nationality
    hdr_row_1 = row
    _write_header_row(row, 'Nationality')
    row += 1
    nat_rows = [
        ('1. Nepalese',     'Nepalese'),
        ('2. Non-Nepalese', None),
        ('2.1 Indian',      'Indian'),
        ('2.2. Chinese',    'Chinese'),
        ('2.3 Australian',  'Australian'),
        ('2.4 Srilanka',    'Srilankan'),
        ('2.5 USA',         'USA'),
        ('2.6 Others',      'Others'),
    ]
    for label, key in nat_rows:
        _write_data_row(row, label, mb_nat.get(key, 0) if key else 0,
                        ips_nat.get(key, 0) if key else 0)
        row += 1
    _write_total_row(row, mb_total, ips_total)
    _autofit(hdr_row_1, row)
    row += 2   # blank separator row

    # Table 2 — Gender
    hdr_row_2 = row
    _write_header_row(row, 'Gender')
    row += 1
    for g in GENDER_BUCKETS:
        _write_data_row(row, g, mb_gender.get(g, 0), ips_gender.get(g, 0))
        row += 1
    _write_total_row(row, mb_total, ips_total)
    _autofit(hdr_row_2, row)
    row += 2

    # Table 3 — Age Group
    hdr_row_3 = row
    _write_header_row(row, 'Age Group')
    row += 1
    age_row_labels = [
        ('Less than or equal to 18 years', '≤18 years'),
        ('19-40 years',                    '19-40 years'),
        ('41-65 years',                    '41-65 years'),
        ('65+years',                       '65+ years'),
    ]
    for label, key in age_row_labels:
        _write_data_row(row, label, mb_age.get(key, 0), ips_age.get(key, 0))
        row += 1
    _write_total_row(row, mb_total, ips_total)
    _autofit(hdr_row_3, row)


# ---------------------------------------------------------------------------
# Top-level helpers called by views
# ---------------------------------------------------------------------------

def mb_path(uid):
    return os.path.join(settings.BASE_DIR, 'media', 'outputs', f'mb_users_{uid}.xlsx')


def ips_path(uid):
    return os.path.join(settings.BASE_DIR, 'media', 'outputs', f'ips_users_{uid}.xlsx')


def run_enrichment(uid, auto_seed=True):
    """Read both files, enrich via CBS, persist rows, return (missing, rows, stats).

    Returns:
        (missing_records, rows, stats_dict) where stats includes invalid_mb_count, invalid_ips_count
    """
    mb, mb_invalid = extract_mb_rows(mb_path(uid))
    ips, ips_invalid = extract_ips_rows(ips_path(uid))
    rows = mb + ips
    enrich_users(rows, auto_seed=auto_seed)
    apply_null_defaults(rows)
    save_rows(uid, rows)
    missing = collect_missing(rows)
    stats = {
        'invalid_mb_count': mb_invalid,
        'invalid_ips_count': ips_invalid,
        'total_invalid': mb_invalid + ips_invalid,
        'total_processed': len(rows),
    }
    return missing, rows, stats


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
