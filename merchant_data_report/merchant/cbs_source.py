"""
cbs_source.py — single CBS data-source abstraction.

ALL code that needs CBS data calls the two functions here:
  • cbs_lookup(account_numbers)      → dict {acc: record}
  • cbs_accounts_in_set(account_numbers) → set of known account numbers

TODAY  — reads from the local CBSMerchant table (populated by load_cbs_excel).
FUTURE — replace the bodies of those two functions with HTTP calls to the
         bank's read-only API endpoint. The return shapes must stay identical;
         nothing else in the codebase needs to change.

Raw CBS data (from DB or future API) is always passed through
normalize_cbs_record() before being returned or stored — province codes,
gender values, country codes, and DOB strings are all cleaned in one place.
"""

from datetime import date
from .models import CBSMerchant

_SQL_CHUNK = 900  # SQLite IN-clause parameter limit


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_cbs_record(raw):
    """
    Clean one raw CBS record.

    Accepts a dict with either CBS export column names (MainCode, PState, …)
    or Django field names (account_number, province, …) — both work.

    Returns a normalised dict with Django field names, ready to be stored
    in CleanCBS or used directly in the pipeline.
    """
    def _clean(val):
        if val is None:
            return None
        s = str(val).strip()
        return None if s.upper() == 'NULL' or s == '' else s

    def _parse_dob(val):
        if isinstance(val, date):
            return val
        s = _clean(val)
        if not s:
            return None
        try:
            return date.fromisoformat(str(s).split(' ')[0])
        except ValueError:
            return None

    def _parse_gender(val):
        s = _clean(val)
        if not s:
            return None
        g = s.upper()
        # M / F / O are valid; J (juridical), NULL, anything else → None
        return g if g in ('M', 'F', 'O') else None

    def _parse_country(val):
        s = _clean(val)
        return s.strip() if s else '01'

    # Nepal province numeric codes → canonical text names
    _PROVINCE_CODES = {
        '1': 'Koshi', '01': 'Koshi', '001': 'Koshi',
        '2': 'Madhesh', '02': 'Madhesh', '002': 'Madhesh',
        '3': 'Bagmati', '03': 'Bagmati', '003': 'Bagmati',
        '4': 'Gandaki', '04': 'Gandaki', '004': 'Gandaki',
        '5': 'Lumbini', '05': 'Lumbini', '005': 'Lumbini',
        '6': 'Karnali', '06': 'Karnali', '006': 'Karnali',
        '7': 'Sudurpaschim', '07': 'Sudurpaschim', '007': 'Sudurpaschim',
    }

    # Accept both CBS export column names and Django field names
    acc   = _clean(raw.get('MainCode')      or raw.get('account_number'))
    prov  = _clean(raw.get('PState')        or raw.get('province'))       or ''
    prov  = _PROVINCE_CODES.get(prov.strip(), prov)   # normalize numeric → text
    dist  = _clean(raw.get('M_DistName')    or raw.get('district'))       or ''
    addr1 = _clean(raw.get('Address1')      or raw.get('address_1'))      or ''
    addr3 = _clean(raw.get('Address3')      or raw.get('address_3'))      or ''
    muni  = _clean(raw.get('municipality')) or ''
    gender = _parse_gender(raw.get('M_Gender')    or raw.get('gender'))
    dob    = _parse_dob(raw.get('DateOfBirth')     or raw.get('dob'))
    cc     = _parse_country(raw.get('CountryCode1') or raw.get('country_code'))

    return {
        'account_number': acc,
        'province':       prov,
        'district':       dist,
        'municipality':   muni,
        'address_1':      addr1,
        'address_3':      addr3,
        'gender':         gender,
        'dob':            dob,
        'country_code':   cc,
    }


# ---------------------------------------------------------------------------
# Data-source functions  ← swap these two for the real API when ready
# ---------------------------------------------------------------------------
#cbs production link ya following 2 function ma change hanne
def cbs_lookup(account_numbers):
    """
    Return CBS data for the given accounts as a normalised dict.

    Returns: { account_number: normalize_cbs_record(…), … }
             Only accounts actually found in CBS are included.

    TODAY:  queries the local CBSMerchant table.
    FUTURE: replace the body with one API call, e.g.:
                response = requests.post(CBS_API_URL, json={'accounts': accs}, ...)
                return {r['account_number']: normalize_cbs_record(r)
                        for r in response.json()['records']}
    """
    accs = [str(a) for a in account_numbers if a and str(a).strip()]
    if not accs:
        return {}

    result = {}
    for i in range(0, len(accs), _SQL_CHUNK):
        chunk = accs[i:i + _SQL_CHUNK]
        for r in CBSMerchant.objects.filter(account_number__in=chunk).values(
            'account_number', 'province', 'district', 'municipality',
            'address_1', 'address_3', 'gender', 'dob', 'country_code'
        ):
            result[r['account_number']] = normalize_cbs_record(r)

    return result


def cbs_accounts_in_set(account_numbers):
    """
    Return the subset of account_numbers that are known to CBS.

    TODAY:  queries CBSMerchant.
    FUTURE: replace with a lightweight API presence-check endpoint.
    """
    accs = [str(a) for a in account_numbers if a and str(a).strip()]
    if not accs:
        return set()

    found = set()
    for i in range(0, len(accs), _SQL_CHUNK):
        chunk = accs[i:i + _SQL_CHUNK]
        found.update(
            CBSMerchant.objects.filter(account_number__in=chunk)
            .values_list('account_number', flat=True)
        )
    return found
