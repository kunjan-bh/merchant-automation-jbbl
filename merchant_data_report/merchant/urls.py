from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_merchant_data, name='upload_merchant_data'),
    path('download/<str:filename>/', views.download_sheet, name='download_sheet'),
]
