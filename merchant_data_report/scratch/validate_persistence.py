import os
import django
import sys

# Setup Django environment
sys.path.append(r'c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

from merchant.models import CleanCBS
from merchant.views import map_province, map_district, map_local, province_from_district

def validate_persistence():
    test_acc_complete = "TEST999999999999999"
    test_acc_incomplete = "TEST888888888888888"

    # Cleanup
    CleanCBS.objects.filter(account_number__in=[test_acc_complete, test_acc_incomplete]).delete()

    print("--- 1. Testing Incomplete Data Persistence ---")
    fields_inc = {'province': 'Bagmati', 'district': 'Kathmandu District'} # Missing Municipality
    
    # Simulate the logic in apply_manual_mapping
    full_data = fields_inc.copy()
    if full_data.get('province') and full_data.get('district') and full_data.get('municipality'):
        CleanCBS.objects.create(account_number=test_acc_incomplete, is_merchant=True, **full_data)
        print("FAIL: Saved incomplete data (this shouldn't happen!)")
    else:
        print("SUCCESS: Rejected incomplete data.")

    print("\n--- 2. Testing Complete Data Persistence ---")
    fields_comp = {'province': 'Bagmati', 'district': 'Kathmandu District', 'municipality': 'MP'}
    
    # Simulate the logic in apply_manual_mapping
    full_data = fields_comp.copy()
    if full_data.get('province') and full_data.get('district') and full_data.get('municipality'):
        CleanCBS.objects.create(account_number=test_acc_complete, is_merchant=True, **full_data)
        print("SUCCESS: Saved complete data.")
    else:
        print("FAIL: Rejected complete data (this should have been saved!)")

    # Verify in DB
    obj = CleanCBS.objects.filter(account_number=test_acc_complete).first()
    if obj and obj.province == 'Bagmati' and obj.district == 'Kathmandu District' and obj.municipality == 'MP':
        print("VERIFIED: Data exists in CleanCBS correctly.")
    else:
        print("ERROR: Data not found or incorrect in CleanCBS.")

    print("\n--- 3. Testing Merged Completeness Persistence ---")
    test_acc_merged = "TEST777777777777777"
    # Pre-create with only one field
    CleanCBS.objects.create(account_number=test_acc_merged, is_merchant=True, municipality='MC')
    
    # Resolved fields (only Province and District)
    resolved_fields = {'province': 'Lumbini', 'district': 'Rupandehi District'}
    
    # Simulate the logic in api_classify_municipality Layer 4
    obj = CleanCBS.objects.filter(account_number=test_acc_merged).first()
    combined = {
        'province': resolved_fields.get('province'),
        'district': resolved_fields.get('district'),
        'municipality': resolved_fields.get('municipality')
    }
    if obj:
        for f in ('province', 'district', 'municipality'):
            if not combined.get(f):
                combined[f] = getattr(obj, f, None)
    
    if combined.get('province') and combined.get('district') and combined.get('municipality'):
        # Update missing fields
        for f in ('province', 'district', 'municipality'):
            v = resolved_fields.get(f)
            if v and not getattr(obj, f, None):
                setattr(obj, f, v)
        obj.save()
        print("SUCCESS: Saved merged complete data.")
    else:
        print("FAIL: Rejected merged complete data.")

    # Verify
    obj = CleanCBS.objects.filter(account_number=test_acc_merged).first()
    if obj and obj.province == 'Lumbini' and obj.district == 'Rupandehi District' and obj.municipality == 'MC':
        print("VERIFIED: Merged data exists in CleanCBS correctly.")
    else:
        print("ERROR: Merged data incorrect.")

    # Cleanup
    CleanCBS.objects.filter(account_number__in=[test_acc_complete, test_acc_incomplete, test_acc_merged]).delete()

if __name__ == "__main__":
    validate_persistence()
