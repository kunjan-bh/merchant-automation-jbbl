from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_merchant_data, name='upload_merchant_data'),
    path('review/<str:unique_id>/', views.review_missing_data, name='review_missing_data'),
    path('apply/<str:unique_id>/', views.apply_manual_mapping, name='apply_manual_mapping'),
    path('finalize/<str:unique_id>/', views.finalize_report, name='finalize_report'),
    path('download/<str:filename>/', views.download_sheet, name='download_sheet'),
    # API endpoints for real-time progress tracking
    path('api/start/', views.api_start, name='api_start'),
    path('api/progress/<str:unique_id>/', views.api_progress, name='api_progress'),
    path('api/finalize/<str:unique_id>/', views.api_finalize, name='api_finalize'),
]
