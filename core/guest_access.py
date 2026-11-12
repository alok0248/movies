"""Guest playback quota: 10 titles per device, then registration.

The app calls these endpoints around playback:

* ``GET  /api/guest/quota/?deviceId=...`` - how many plays are left, so the app
  can show the countdown before the user starts something.
* ``POST /api/guest/play/`` - consume one play. Registered users with a verified
  email are unlimited; a guest at the limit gets ``limit_reached`` and the app
  blocks playback with a registration prompt.

An email is only treated as registered here when the account exists and its
email is verified, so a device cannot escape the limit by inventing an address.
"""

import json

from django.http import JsonResponse
from django.utils import timezone

from .models import GuestPlayQuota, SyncedUser
from .views import _validate_android_auth

DEFAULT_LIMIT = 10


def _quota_payload(quota, unlimited=False):
    return {
        'status': 'ok',
        'unlimited': unlimited,
        'limit': None if unlimited else quota.limit if quota else DEFAULT_LIMIT,
        'used': 0 if unlimited else (quota.plays_used if quota else 0),
        'remaining': None if unlimited else (quota.remaining if quota else DEFAULT_LIMIT),
        'blocked': False if unlimited else bool(quota and quota.remaining <= 0),
    }


def _verified_account(email):
    if not email:
        return None
    return SyncedUser.objects.filter(
        email__iexact=email, is_email_verified=True).first()


def _get_quota(device_id):
    quota, _created = GuestPlayQuota.objects.get_or_create(
        device_id=device_id, defaults={'limit': DEFAULT_LIMIT})
    return quota


def api_guest_quota(request):
    """GET /api/guest/quota/?deviceId=... — plays left on this device."""
    _app_match, err = _validate_android_auth(request)
    if err:
        return err

    device_id = (request.GET.get('deviceId') or '').strip()[:100]
    email = (request.GET.get('email') or '').strip().lower()
    if not device_id:
        return JsonResponse({'status': 'error', 'message': 'deviceId is required'}, status=400)

    if _verified_account(email):
        return JsonResponse(_quota_payload(None, unlimited=True))

    return JsonResponse(_quota_payload(_get_quota(device_id)))


def api_guest_play(request):
    """POST /api/guest/play/ {deviceId, email?, titleId?, titleType?} — use a play."""
    _app_match, err = _validate_android_auth(request)
    if err:
        return err
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)

    try:
        body = json.loads(request.body or '{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON body'}, status=400)

    device_id = (body.get('deviceId') or '').strip()[:100]
    email = (body.get('email') or '').strip().lower()
    if not device_id:
        return JsonResponse({'status': 'error', 'message': 'deviceId is required'}, status=400)

    if _verified_account(email):
        return JsonResponse(_quota_payload(None, unlimited=True))

    quota = _get_quota(device_id)
    if quota.remaining <= 0:
        payload = _quota_payload(quota)
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

    payload = _quota_payload(quota)
    payload['status'] = 'ok'
    return JsonResponse(payload)
