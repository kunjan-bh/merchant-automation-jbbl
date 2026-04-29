from django.urls import path
from . import views
from . import auth

urlpatterns = [
    # ── Auth + Dashboard ──────────────────────────────────────────────
    path('',          auth.login_view,     name='login'),
    path('logout/',   auth.logout_view,    name='logout'),
    path('dashboard/', auth.dashboard,     name='dashboard'),

    # ── Custom admin log viewer (NOT Django admin) ────────────────────
    path('admin/',                     auth.admin_logs,          name='admin_logs'),
    path('admin/log/<str:filename>/',  auth.admin_download_log,  name='admin_download_log'),

    # ── User management (admin only) ──────────────────────────────────
    path('admin/users/create/',                     auth.admin_user_create,        name='admin_user_create'),
    path('admin/users/<int:pk>/toggle-admin/',     auth.admin_user_toggle_admin,  name='admin_user_toggle_admin'),
    path('admin/users/<int:pk>/toggle-active/',    auth.admin_user_toggle_active, name='admin_user_toggle_active'),
    path('admin/users/<int:pk>/reset-password/',   auth.admin_user_reset_password,name='admin_user_reset_password'),
    path('admin/users/<int:pk>/delete/',           auth.admin_user_delete,        name='admin_user_delete'),
    path('account/change-password/',               auth.change_password_view,     name='change_password'),

    # ── Batch-level actions ───────────────────────────────────────────
    path('verify/<int:pk>/',       auth.toggle_verified, name='toggle_verified'),
    path('download/final/<int:pk>/', auth.download_final, name='download_final'),
    path('download/explanation/<int:pk>/', views.download_explanation, name='download_explanation'),

    # ── Existing pipeline (login-gated) ───────────────────────────────
    path('upload/',                          views.upload_merchant_data, name='upload_merchant_data'),
    path('review/<str:unique_id>/',          views.review_missing_data,  name='review_missing_data'),
    path('apply/<str:unique_id>/',           views.apply_manual_mapping, name='apply_manual_mapping'),
    path('users-review/<str:unique_id>/',    views.users_review,         name='users_review'),
    path('users-apply/<str:unique_id>/',     views.users_apply,          name='users_apply'),
    path('payment-review/<str:unique_id>/',  views.payment_detail_review,name='payment_detail_review'),
    path('payment-apply/<str:unique_id>/',   views.payment_detail_apply, name='payment_detail_apply'),
    path('finalize/<str:unique_id>/',        views.finalize_report,      name='finalize_report'),
    path('review-download/<str:unique_id>/', views.download_review_list, name='download_review_list'),
    path('download/<str:filename>/',         views.download_sheet,       name='download_sheet'),

    # ── API endpoints ─────────────────────────────────────────────────
    path('api/analytics/',                      views.analytics_api,            name='analytics_api'),
    path('api/validate-file/',                  views.api_validate_file,        name='api_validate_file'),
    path('api/start/',                          views.api_start,                name='api_start'),
    path('api/progress/<str:unique_id>/',       views.api_progress,             name='api_progress'),
    path('api/finalize/<str:unique_id>/',       views.api_finalize,             name='api_finalize'),
    path('api/classify-municipality/',          views.api_classify_municipality,name='api_classify_municipality'),
]
