"""
User-activity logging.

Every significant action goes to two places:
  1. logs/activity.log — file, rotated daily (90-day backup)
  2. UserActivityLog     — database table (surfaced in /admin log viewer)

Use the typed wrappers (log_login, log_generate_*, log_download, log_verify)
from views so action strings stay consistent.
"""

import logging
import os
from datetime import datetime
from django.conf import settings


LOG_DIR = os.path.join(settings.BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

_file_logger = logging.getLogger('merchant.activity')


def _get_ip(request):
    if not request:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')


def log_activity(request, action, detail='', level='INFO', batch=None, user=None, full_name=None):
    if user is None and request:
        user = request.session.get('user', 'anonymous')
    if user is None:
        user = 'system'
    if full_name is None and request:
        full_name = request.session.get('full_name', '')

    ip = _get_ip(request)
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    line = f'[{ts}] [{level:<7}] [{user:<20}] [{ip or "unknown":<15}] {action} | {detail}'
    if level == 'ERROR':
        _file_logger.error(line)
    elif level == 'WARNING':
        _file_logger.warning(line)
    else:
        _file_logger.info(line)

    try:
        from .models import UserActivityLog
        UserActivityLog.objects.create(
            username   = user[:100],
            full_name  = (full_name or '')[:200],
            ip_address = ip,
            level      = level,
            action     = action,
            detail     = detail[:2000],
            batch      = batch,
        )
    except Exception:
        pass


# ── Convenience wrappers ────────────────────────────────────────────────────

def log_login(request, username):
    full_name = request.session.get('full_name', '') if request else ''
    log_activity(request, 'LOGIN', f'Logged in from {_get_ip(request)}',
                 user=username, full_name=full_name)


def log_logout(request, username):
    log_activity(request, 'LOGOUT', 'Session ended', user=username)


def log_login_fail(request, username):
    log_activity(request, 'LOGIN_FAIL',
                 f'Failed login attempt for "{username}" from {_get_ip(request)}',
                 level='WARNING', user=username)


def log_generate_start(request, month, year, batch=None):
    log_activity(request, 'GENERATE_START',
                 f'Started generation for {month} {year}', batch=batch)


def log_generate_success(request, batch):
    log_activity(
        request, 'GENERATE_SUCCESS',
        f'{batch.month} {batch.year} — {batch.total_records} records '
        f'(fonepay {batch.fonepay_count}, nepalpay {batch.nepalpay_count})',
        batch=batch,
    )


def log_generate_error(request, batch, error):
    log_activity(
        request, 'GENERATE_ERROR',
        f'{batch.month} {batch.year} — {str(error)[:500]}',
        level='ERROR', batch=batch,
    )


def log_download(request, batch, filename):
    log_activity(
        request, 'DOWNLOAD',
        f'{filename} (batch #{batch.pk if batch else "—"})',
        batch=batch,
    )


def log_verify(request, batch, verified):
    log_activity(
        request,
        'VERIFY' if verified else 'UNVERIFY',
        f'{batch.month} {batch.year} — batch #{batch.pk}',
        batch=batch,
    )
