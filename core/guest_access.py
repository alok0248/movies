"""Guest playback quota: N titles per device, then registration.

The app calls these endpoints around playback:

* ``GET  /api/guest/quota/?deviceId=...`` - how many plays are left, so the app
  can show the countdown before the user starts something.
* ``POST /api/guest/play/`` - consume one play. Registered users with a verified
  email are unlimited; a guest at the limit gets ``limit_reached`` and the app
  blocks playback with a registration prompt.

The allowance is ``no_login_play`` from the app's edit page (Android apps ->
edit -> App behaviour), and 10 when that key is not set. It is read from the app
row that authenticated the call, so an admin change reaches the next quota call
without an app release. Existing devices keep the plays they have used: raising
the number hands them the difference, lowering it can put them over the line.

An email is only treated as registered here when the account exists and its
email is verified, so a device cannot escape the limit by inventing an address.
"""

import json

from django.http import JsonResponse
from django.utils import timezone

from .models import GuestPlayQuota, SyncedUser
from .views import _validate_android_auth

DEFAULT_LIMIT = 10
MAX_LIMIT = 1000


def limit_for_app(app):
    """The ``no_login_play`` value saved for this app, or the 10 default."""
    payload = getattr(app, 'json_payload', None)
    if not isinstance(payload, dict):
        return DEFAULT_LIMIT
    raw = payload.get('no_login_play', payload.get('noLoginPlay'))
    if isinstance(raw, bool):
        value = int(raw)
    elif isinstance(raw, (int, float)):
        value = int(raw)
    elif isinstance(raw, str):
        digits = ''.join(ch for ch in raw if ch.isdigit())
        value = int(digits[:6]) if digits else DEFAULT_LIMIT
    else:
        return DEFAULT_LIMIT
    return max(0, min(MAX_LIMIT, value))


def _quota_payload(quota, unlimited=False, limit=DEFAULT_LIMIT):
    return {
        'status': 'ok',
        'unlimited': unlimited,
        'limit': None if unlimited else (quota.limit if quota else limit),
        'used': 0 if unlimited else (quota.plays_used if quota else 0),
        'remaining': None if unlimited else (quota.remaining if quota else limit),
        'blocked': False if unlimited else bool(quota and quota.remaining <= 0),
    }


def _verified_account(email):
    if not email:
        return None
    return SyncedUser.objects.filter(
        email__iexact=email, is_email_verified=True).first()


def _get_quota(device_id, limit=DEFAULT_LIMIT):
    quota, _created = GuestPlayQuota.objects.get_or_create(
        device_id=device_id, defaults={'limit': limit})
    if quota.limit != limit:
        # The admin changed the allowance: keep the plays already used, so the
        # device gets the new number minus what it has spent.
        quota.limit = limit
        quota.save(update_fields=['limit'])
    return quota


def api_guest_quota(request):
    """GET /api/guest/quota/?deviceId=... — plays left on this device."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    limit = limit_for_app(app_match)

    device_id = (request.GET.get('deviceId') or '').strip()[:100]
    email = (request.GET.get('email') or '').strip().lower()
    if not device_id:
        return JsonResponse({'status': 'error', 'message': 'deviceId is required'}, status=400)

    if _verified_account(email):
        return JsonResponse(_quota_payload(None, unlimited=True, limit=limit))

    return JsonResponse(_quota_payload(_get_quota(device_id, limit), limit=limit))


def api_guest_play(request):
    """POST /api/guest/play/ {deviceId, email?, titleId?, titleType?} — use a play."""
    app_match, err = _validate_android_auth(request)
    if err:
        return err
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    limit = limit_for_app(app_match)

    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    device_id = (body.get('deviceId') or '').strip()[:100]
    email = (body.get('email') or '').strip().lower()
    if not device_id:
        return JsonResponse({'status': 'error', 'message': 'deviceId is required'}, status=400)

    if _verified_account(email):
        return JsonResponse(_quota_payload(None, unlimited=True, limit=limit))

    quota = _get_quota(device_id, limit)
    if quota.remaining <= 0:
        payload = _quota_payload(quota, limit=limit)
        payload.update({
            'status': 'limit_reached',
            'message': 'Free previews used up on this device. Create an account to keep watching.',
        })
        return JsonResponse(payload, status=403)

    quota.plays_used += 1
    quota.last_play_at = timezone.now()
    quota.last_email = email
    quota.last_title = str(body.get('titleId') or '')[:100]
    quota.save(update_fields=['plays_used', 'last_play_at', 'last_email', 'last_title'])

    payload = _quota_payload(quota, limit=limit)
    payload['status'] = 'ok'
    return JsonResponse(payload)
