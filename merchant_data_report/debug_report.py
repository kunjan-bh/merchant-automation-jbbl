import os, sys, django
sys.path.append(r"c:\Users\Lenovo\Desktop\Monthly report\merchant data\code\code,3,4,5,6\merchant_data_report")
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'merchant_data_report.settings')
django.setup()

import merchant.views
merchant.views._generate_final_report('c5c5a4f8')
