"""
Session-based auth with hardcoded users (no Django auth).

`login_required_custom` and `admin_required` decorators gate views against
`request.session['user']`. The dashboard, login/logout, and /admin log
viewer all live here to keep auth concerns in one place.
"""

import os
import re
from functools import wraps
from datetime import datetime

from django.conf import settings
from django.core.paginator import Paginator
from django.http import HttpResponse, Http404, FileResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.contrib import messages

from .logger import (
    log_login, log_logout, log_login_fail, log_verify, log_activity,
)
from .models import ReportBatch, UserActivityLog, SystemUser, CleanCBS


# ──────────────────────────────────────────────────────────────────────────
# DECORATORS
# ──────────────────────────────────────────────────────────────────────────

def login_required_custom(view):
    @wraps(view)
    def _wrapper(request, *args, **kwargs):
        if not request.session.get('user'):
            return redirect('login')
        return view(request, *args, **kwargs)
    return _wrapper


def admin_required(view):
    @wraps(view)
    def _wrapper(request, *args, **kwargs):
        user = request.session.get('user')
        if not user:
            return redirect('login')
        if not request.session.get('is_admin', False):
            return HttpResponse('Forbidden — admin access only.', status=403)
        return view(request, *args, **kwargs)
    return _wrapper


# ──────────────────────────────────────────────────────────────────────────
# LOGIN / LOGOUT
# ──────────────────────────────────────────────────────────────────────────

def login_view(request):
    # Already logged in? Go to dashboard.
    if request.session.get('user'):
        return redirect('dashboard')

    error = None
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''

        try:
            user = SystemUser.objects.get(username=username, is_active=True)
            if user.check_password(password):
                request.session['user']      = username
                request.session['full_name'] = user.full_name
                request.session['is_admin']  = user.is_admin
                request.session.set_expiry(86400)  # 24h
                log_login(request, username)
                return redirect('dashboard')
            else:
                error = 'Invalid username or password.'
                log_login_fail(request, username)
        except SystemUser.DoesNotExist:
            error = 'Invalid username or password.'
            log_login_fail(request, username)

    return render(request, 'merchant/login.html', {'error': error})


def logout_view(request):
    username = request.session.get('user')
    if username:
        log_logout(request, username)
    request.session.flush()
    return redirect('login')


# ──────────────────────────────────────────────────────────────────────────
# DASHBOARD (3 panels)
# ──────────────────────────────────────────────────────────────────────────

BS_MONTHS = ['Baisakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra',
             'Ashwin',  'Kartik', 'Mangsir', 'Poush',   'Magh',
             'Falgun',  'Chaitra']

BS_YEARS = list(range(2080, 2091))


@login_required_custom
def dashboard(request):
    user      = request.session['user']
    full_name = request.session.get('full_name', user)
    is_admin  = request.session.get('is_admin', False)

    shared_qs = ReportBatch.objects.filter(
        status='completed', has_errors=False, verified=True,
    )
    my_qs = ReportBatch.objects.filter(generated_by=user)

    shared_page = Paginator(shared_qs, 6).get_page(request.GET.get('sh_page'))
    my_page     = Paginator(my_qs,     6).get_page(request.GET.get('my_page'))

    return render(request, 'merchant/dashboard.html', {
        'user':             user,
        'full_name':        full_name,
        'is_admin':         is_admin,
        'institution_name': getattr(settings, 'INSTITUTION_NAME', ''),
        'institution_code': getattr(settings, 'INSTITUTION_CODE', ''),
        'bs_months':        BS_MONTHS,
        'bs_years':         BS_YEARS,
        'shared_page':      shared_page,
        'my_page':          my_page,
    })


# ──────────────────────────────────────────────────────────────────────────
# VERIFY TOGGLE (officer marks own completed, error-free batch as shared)
# ──────────────────────────────────────────────────────────────────────────

@login_required_custom
@require_POST
def toggle_verified(request, pk):
    user = request.session['user']
    is_admin = request.session.get('is_admin', False)
    try:
        batch = ReportBatch.objects.get(pk=pk)
    except ReportBatch.DoesNotExist:
        raise Http404

    # Only the officer who generated it can verify (admin override allowed)
    if batch.generated_by != user and not is_admin:
        return HttpResponse('Forbidden', status=403)

    if batch.status != 'completed' or batch.has_errors:
        messages.error(request, 'Only completed, error-free reports can be verified.')
        return redirect(request.POST.get('next') or 'dashboard')

    batch.verified = not batch.verified
    batch.save(update_fields=['verified'])
    log_verify(request, batch, batch.verified)

    messages.success(
        request,
        'Report marked as error-free and published.' if batch.verified
        else 'Report unverified — removed from Passed Reports.',
    )
    return redirect(request.POST.get('next') or 'dashboard')


# ──────────────────────────────────────────────────────────────────────────
# DOWNLOAD FINAL REPORT (via batch)
# ──────────────────────────────────────────────────────────────────────────

@login_required_custom
def download_final(request, pk):
    try:
        batch = ReportBatch.objects.get(pk=pk)
    except ReportBatch.DoesNotExist:
        raise Http404
    if not batch.final_report_filename:
        raise Http404('Final report not yet generated.')

    file_path = os.path.join(settings.BASE_DIR, 'media', 'outputs', batch.final_report_filename)
    if not os.path.exists(file_path):
        raise Http404('File missing on disk.')

    from .logger import log_download
    log_download(request, batch, batch.final_report_filename)
    return FileResponse(open(file_path, 'rb'), as_attachment=True,
                        filename=batch.final_report_filename)


# ──────────────────────────────────────────────────────────────────────────
# USER MANAGEMENT (admin only)
# ──────────────────────────────────────────────────────────────────────────

@admin_required
@require_POST
def admin_user_create(request):
    """Create a new user with auto-generated password."""
    username  = (request.POST.get('username') or '').strip()
    full_name = (request.POST.get('full_name') or '').strip()
    is_admin  = request.POST.get('is_admin') == 'on'

    if not username:
        messages.error(request, 'Username is required.')
        return redirect('/admin/#users')

    if SystemUser.objects.filter(username=username).exists():
        messages.error(request, 'Username already exists.')
        return redirect('/admin/#users')

    # Generate password
    raw_password = SystemUser.generate_password()
    user = SystemUser(
        username=username,
        full_name=full_name,
        is_admin=is_admin,
    )
    user.set_password(raw_password)
    user.save()

    # Store password in session for one-time display
    request.session['new_user_password'] = {'username': username, 'password': raw_password}

    log_activity(request, 'USER_CREATE', f'Created user {username}')
    messages.success(request, f'User {username} created successfully.')
    return redirect('/admin/#users')


@admin_required
@require_POST
def admin_user_toggle_admin(request, pk):
    """Toggle admin role for a user."""
    try:
        user = SystemUser.objects.get(pk=pk)
    except SystemUser.DoesNotExist:
        raise Http404

    # Protect primary admin
    if user.username == 'admin':
        messages.error(request, 'Cannot modify the primary admin account.')
        return redirect('/admin/#users')

    user.is_admin = not user.is_admin
    user.save(update_fields=['is_admin'])

    action = 'USER_ROLE_CHANGE'
    detail = f'Set admin role to {user.is_admin} for {user.username}'
    log_activity(request, action, detail)

    role_text = 'Admin' if user.is_admin else 'User'
    messages.success(request, f'{user.username} is now a {role_text}.')
    return redirect('/admin/#users')


@admin_required
@require_POST
def admin_user_toggle_active(request, pk):
    """Toggle active status for a user."""
    try:
        user = SystemUser.objects.get(pk=pk)
    except SystemUser.DoesNotExist:
        raise Http404

    # Protect primary admin
    if user.username == 'admin':
        messages.error(request, 'Cannot deactivate the primary admin account.')
        return redirect('/admin/#users')

    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])

    action = 'USER_DEACTIVATE' if not user.is_active else 'USER_ACTIVATE'
    detail = f'User {user.username} is now {"inactive" if not user.is_active else "active"}'
    log_activity(request, action, detail)

    status_text = 'deactivated' if not user.is_active else 'reactivated'
    messages.success(request, f'{user.username} has been {status_text}.')
    return redirect('/admin/#users')


@admin_required
@require_POST
def admin_user_reset_password(request, pk):
    """Reset password for a user and show it once."""
    try:
        user = SystemUser.objects.get(pk=pk)
    except SystemUser.DoesNotExist:
        raise Http404

    # Protect primary admin from password reset
    if user.username == 'admin':
        messages.error(request, 'Cannot reset password for the primary admin account.')
        return redirect('/admin/#users')

    # Generate new password
    raw_password = SystemUser.generate_password()
    user.set_password(raw_password)
    user.save(update_fields=['password'])

    # Store in session for one-time display
    request.session['new_user_password'] = {'username': user.username, 'password': raw_password}

    log_activity(request, 'PASSWORD_RESET', f'Password reset for {user.username}')
    messages.success(request, f'Password reset for {user.username}.')
    return redirect('/admin/#users')


@admin_required
@require_POST
def admin_user_delete(request, pk):
    """Delete a user account."""
    try:
        user = SystemUser.objects.get(pk=pk)
    except SystemUser.DoesNotExist:
        raise Http404

    # Protect primary admin from deletion
    if user.username == 'admin':
        messages.error(request, 'Cannot delete the primary admin account.')
        return redirect('/admin/#users')

    username = user.username
    user.delete()

    log_activity(request, 'USER_DELETE', f'Deleted user {username}')
    messages.error(request, f'{username} has been deleted.')
    return redirect('/admin/#users')


@login_required_custom
@require_POST
def change_password_view(request):
    """Allow user to change their own password."""
    username = request.session['user']
    current_password = request.POST.get('current_password') or ''
    new_password = request.POST.get('new_password') or ''
    confirm_password = request.POST.get('confirm_password') or ''

    try:
        user = SystemUser.objects.get(username=username)
    except SystemUser.DoesNotExist:
        messages.error(request, 'User not found.')
        return redirect('dashboard')

    # Validate current password
    if not user.check_password(current_password):
        messages.error(request, 'Current password is incorrect.')
        return redirect('dashboard')

    # Validate new password
    if new_password != confirm_password:
        messages.error(request, 'New passwords do not match.')
        return redirect('dashboard')

    if len(new_password) < 6:
        messages.error(request, 'New password must be at least 6 characters.')
        return redirect('dashboard')

    # Update password
    user.set_password(new_password)
    user.save(update_fields=['password'])

    log_activity(request, 'PASSWORD_CHANGE', f'Password changed by {username}')
    messages.success(request, 'Password changed successfully. Please log in again.')
    return redirect('logout')


# ──────────────────────────────────────────────────────────────────────────
# CUSTOM /admin — log viewer (admin only, NOT Django admin)
# ──────────────────────────────────────────────────────────────────────────

_LOG_LINE = re.compile(
    r'^\[(?P<ts>[\d\-:\s]+)\] \[(?P<level>\w+)\s*\] \[(?P<user>[^\]]+)\] '
    r'\[(?P<ip>[^\]]+)\]\s+(?P<action>\w+)\s*\|\s*(?P<detail>.*)$'
)


def _parse_log_file(path):
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                m = _LOG_LINE.match(line.rstrip())
                if m:
                    rows.append({
                        'ts':     m.group('ts').strip(),
                        'level':  m.group('level').strip(),
                        'user':   m.group('user').strip(),
                        'ip':     m.group('ip').strip(),
                        'action': m.group('action').strip(),
                        'detail': m.group('detail').strip(),
                    })
    except Exception:
        pass
    return rows


@admin_required
def admin_logs(request):
    """Custom admin log viewer — reads DB (UserActivityLog) with filters + lists rotated files."""
    qs = UserActivityLog.objects.all()

    # Filters
    f_user   = request.GET.get('user',   '').strip()
    f_level  = request.GET.get('level',  '').strip()
    f_action = request.GET.get('action', '').strip()
    f_date   = request.GET.get('date',   '').strip()
    f_q      = request.GET.get('q',      '').strip()

    if f_user:   qs = qs.filter(username__iexact=f_user)
    if f_level:  qs = qs.filter(level=f_level)
    if f_action: qs = qs.filter(action=f_action)
    if f_date:
        try:
            d = datetime.strptime(f_date, '%Y-%m-%d').date()
            qs = qs.filter(timestamp__date=d)
        except ValueError:
            pass
    if f_q:
        from django.db.models import Q
        qs = qs.filter(Q(detail__icontains=f_q) | Q(ip_address__icontains=f_q))

    page = Paginator(qs, 50).get_page(request.GET.get('page'))

    # Error batches
    error_batches = ReportBatch.objects.filter(status='error').order_by('-created_at')[:50]

    # System users
    system_users = SystemUser.objects.all().order_by('username')

    # Clean CBS records — merchant accounts only (paginated)
    clean_cbs_qs = CleanCBS.objects.filter(is_merchant=True).order_by('-account_number')
    clean_cbs_page = Paginator(clean_cbs_qs, 50).get_page(request.GET.get('cbs_page'))

    # Generated password (shown once)
    new_user_password = request.session.pop('new_user_password', None)

    # Rotated log files
    log_dir = os.path.join(settings.BASE_DIR, 'logs')
    log_files = []
    if os.path.isdir(log_dir):
        for name in sorted(os.listdir(log_dir), reverse=True):
            fpath = os.path.join(log_dir, name)
            if os.path.isfile(fpath):
                log_files.append({
                    'name': name,
                    'size_kb': round(os.path.getsize(fpath) / 1024, 1),
                })

    all_users = sorted({u for u in UserActivityLog.objects.values_list('username', flat=True).distinct()})

    return render(request, 'merchant/admin_logs.html', {
        'page':                page,
        'error_batches':       error_batches,
        'system_users':        system_users,
        'clean_cbs_page':      clean_cbs_page,
        'new_user_password':   new_user_password,
        'log_files':           log_files,
        'all_users':           all_users,
        'level_choices':       UserActivityLog.LEVEL_CHOICES,
        'action_choices':      UserActivityLog.ACTION_CHOICES,
        'filters': {
            'user': f_user, 'level': f_level, 'action': f_action,
            'date': f_date, 'q': f_q,
        },
        'full_name': request.session.get('full_name', 'admin'),
    })


@admin_required
def admin_download_log(request, filename):
    # Protect against path traversal
    if '/' in filename or '\\' in filename or '..' in filename:
        raise Http404
    path = os.path.join(settings.BASE_DIR, 'logs', filename)
    if not os.path.exists(path):
        raise Http404
    log_activity(request, 'DOWNLOAD', f'Downloaded log file {filename}')
    return FileResponse(open(path, 'rb'), as_attachment=True, filename=filename)
