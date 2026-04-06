from django.contrib import admin
from .models import CBSMerchant

@admin.register(CBSMerchant)
class CBSMerchantAdmin(admin.ModelAdmin):
    list_display = ('merchant_code', 'merchant_id', 'account_number', 'province', 'district', 'municipality')
    search_fields = ('merchant_code', 'merchant_id', 'account_number', 'province', 'district', 'municipality')
    list_filter = ('gender', 'province', 'district')
