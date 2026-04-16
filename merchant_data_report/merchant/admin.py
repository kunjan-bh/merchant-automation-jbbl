from django.contrib import admin
from .models import CBSMerchant, CleanCBS

@admin.register(CBSMerchant)
class CBSMerchantAdmin(admin.ModelAdmin):
    list_display = ('account_number', 'province', 'district', 'municipality','address_3', 'address_1', 'gender', 'dob', 'country_code')
    search_fields = ('account_number', 'province', 'district', 'municipality')
    list_filter = ('gender', 'province', 'district')

@admin.register(CleanCBS)
class CleanCBSAdmin(admin.ModelAdmin):
    list_display = ('account_number', 'is_merchant', 'province', 'district', 'municipality','address_3', 'address_1', 'gender', 'dob', 'country_code')
    search_fields = ('account_number', 'province', 'district', 'municipality')
    list_filter = ('is_merchant', 'gender', 'province', 'district')
