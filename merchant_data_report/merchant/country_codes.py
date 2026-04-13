"""
Country-code → nationality mapping for CBS records.

Kept data-driven so new country codes can be added without touching pipeline code.
Sheet 11 writer always renders the full NATIONALITY_ROWS list; codes not present in
COUNTRY_CODE_MAP fall through to 'Others'.
"""
from datetime import date


# Authoritative mapping. Extend here as new codes become known.
COUNTRY_CODE_MAP = {
    '01': 'Nepalese',
    '11': 'Indian',
    '41': 'Others',
}

# Row order used by sheet 11. Codes not yet mapped simply render as 0.
NATIONALITY_ROWS = [
    'Nepalese',
    'Indian',
    'Chinese',
    'Australian',
    'Srilankan',
    'USA',
    'Others',
]


def nationality_from_code(country_code):
    """Return the nationality label for a CBS country code. Unknown → 'Others'."""
    if country_code is None:
        return 'Others'
    return COUNTRY_CODE_MAP.get(str(country_code).strip(), 'Others')


def calculate_age(dob):
    """Derive age (int) from a date-of-birth. None-safe."""
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def age_bucket(age):
    """Bucket label used by sheet 11 age table."""
    if age is None:
        return None
    age = int(age)
    if age <= 18:
        return '≤18 years'
    if age <= 40:
        return '19-40 years'
    if age <= 65:
        return '41-65 years'
    return '65+ years'


AGE_BUCKETS = ['≤18 years', '19-40 years', '41-65 years', '65+ years']
GENDER_BUCKETS = ['Male', 'Female', 'Other', 'Company']


def classify_gender(raw):
    """Map a raw CBS/file gender value into the sheet-11 gender buckets."""
    g = str(raw or '').strip().upper()
    if g in ('M', 'MALE'):
        return 'Male'
    if g in ('F', 'FEMALE'):
        return 'Female'
    if g in ('O', 'OTHER', 'OTHERS'):
        return 'Other'
    # J / NULL / blank / unknown → Company (per project decision)
    return 'Company'
