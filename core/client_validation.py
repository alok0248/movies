"""Device and app checks on the Android API traffic.

**Read this first: what these checks can and cannot prove.**

Every signal used here except the shared Basic credential and the per-user JWT
is a *header the client writes*. A client that wants to lie can set
``X-Android-Installer: com.android.vending`` and
``X-Android-Signature-Sha256: <the real fingerprint>`` by hand. So this is a
bar-raiser, not cryptographic proof:

* It stops the easy cases — a scripted scraper replaying the API, a browser
  replaying a captured request, a sideloaded clone, an emulator farm — because
  each of those has to first learn and then reproduce a set of values that are
  only ever sent by the real app.
* It cannot stop a determined attacker who can read the APK, because a value
  the client can read is a value the client can repeat. HMAC request signing
  (``require_request_signature``) raises that bar further, and a per-user token
  still limits what a copied credential is worth — but the only way to get real
  attestation is Google Play Integrity API, where Google signs a verdict the
  client cannot forge. ``expose_integrity_verdict`` below is where that verdict
  belongs when the app is updated to request one.

**Why nothing is blocked by default.** The checks need headers the shipped
build does not send yet. ``AndroidApp.strict_client_validation`` therefore
starts off: every request is checked, every refusal is recorded (with
``would_block`` set), and the answer is only enforced once an operator turns
the switch on for that app. That makes a trial run readable *before* anything
starts failing — which is what makes switching it on safe.

The chain, in the order it runs (first failure ends the request):

=====================================  ==============================
check                                  reason code
=====================================  ==============================
claimed app slug vs the credential's   ``app_identity_mismatch``
declared platform is android           ``missing_platform_header`` / ``platform_not_android``
no browser-only headers                 ``browser_client``
package name in the allow-list          ``missing_package_header`` / ``package_mismatch``
installer in the allow-list             ``missing_installer_header`` / ``installer_not_allowed``
signing fingerprint in the allow-list   ``missing_signature_header`` / ``signature_mismatch``
client did not report tampering         ``tamper_detected``
not a debuggable build                  ``debug_build``
not an emulator                         ``emulator_detected``
not a rooted device                     ``rooted_device``
HMAC request signature (optional)       ``missing_request_signature`` / ``stale_request``
                                        / ``replayed_request`` / ``invalid_request_signature``
=====================================  ==============================

Every decision is written to :class:`~core.models.AppClientValidationLog`
(refusals always, allows too once strict mode is on) and to the ``core.security``
logger, so the operator has a record of what was let in as well as what was
turned away.
"""
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field

from django.core.cache import cache

logger = logging.getLogger('core.security')

# How old a signed request may be, in seconds, before it is refused as stale.
# The same window is used to remember a nonce, so a captured request cannot be
# replayed inside it.
MAX_CLOCK_SKEW_SECONDS = 300

# Header names the app may send. Everything here is client-asserted.
HEADER_PLATFORM = 'HTTP_X_ANDROID_PLATFORM'
HEADER_PACKAGE = 'HTTP_X_ANDROID_PACKAGE'
HEADER_INSTALLER = 'HTTP_X_ANDROID_INSTALLER'
HEADER_SIGNATURE_SHA256 = 'HTTP_X_ANDROID_SIGNATURE_SHA256'
HEADER_TAMPERED = 'HTTP_X_ANDROID_TAMPERED'
HEADER_DEBUG = 'HTTP_X_ANDROID_DEBUG'
HEADER_EMULATOR = 'HTTP_X_ANDROID_EMULATOR'
HEADER_ROOTED = 'HTTP_X_ANDROID_ROOTED'
HEADER_APP_SLUG = 'HTTP_X_ANDROID_APP'
HEADER_BUILD = 'HTTP_X_ANDROID_BUILD'
HEADER_DEVICE_MODEL = 'HTTP_X_ANDROID_DEVICE'
HEADER_OS_VERSION = 'HTTP_X_ANDROID_OS_VERSION'
HEADER_SIGNATURE = 'HTTP_X_ANDROID_SIGNATURE'
HEADER_TIMESTAMP = 'HTTP_X_ANDROID_TIMESTAMP'
HEADER_NONCE = 'HTTP_X_ANDROID_NONCE'

# Reason codes -> (HTTP status, message). Documented in
# website/docs/android-client-validation.md; keep the two in step.
REASONS = {
    'app_identity_mismatch': (403, 'This request names a different app than its credentials belong to.'),
    'missing_platform_header': (403, 'This request did not declare its platform.'),
    'platform_not_android': (403, 'This endpoint only serves the Android app.'),
    'browser_client': (403, 'This endpoint only serves the Android app, not a web browser.'),
    'missing_package_header': (403, 'This request did not declare its package name.'),
    'package_mismatch': (403, 'This request did not come from a recognised build of the app.'),
    'missing_installer_header': (403, 'This request did not declare where the app was installed from.'),
    'installer_not_allowed': (403, 'This app was not installed from an approved source.'),
    'missing_signature_header': (403, 'This request did not declare its signing certificate.'),
    'signature_mismatch': (403, 'This build is not signed by the official certificate.'),
    'tamper_detected': (403, 'This build reports that it has been modified.'),
    'debug_build': (403, 'Debuggable builds are not accepted.'),
    'emulator_detected': (403, 'Emulator traffic is not accepted.'),
    'rooted_device': (403, 'Rooted devices are not accepted.'),
    'missing_request_signature': (401, 'This request was not signed.'),
    'stale_request': (401, 'This request is too old to accept.'),
    'replayed_request': (401, 'This request has already been used.'),
    'invalid_request_signature': (401, 'The request signature did not match.'),
    'signature_not_configured': (500, 'Request signing is required but no secret is configured.'),
}

# Headers a real native app never sends and a browser always does. Their
# presence is strong evidence the request came from a page, not from the app.
BROWSER_ONLY_HEADERS = ('HTTP_ORIGIN', 'HTTP_SEC_FETCH_MODE', 'HTTP_SEC_FETCH_SITE')


@dataclass
class ValidationResult:
    """The verdict for one request, plus what the client claimed."""

    allowed: bool = True
    code: str = ''
    status: int = 200
    message: str = ''
    claims: dict = field(default_factory=dict)

    def refuse(self, code):
        """Mark the verdict as a refusal and stop (returns False for chaining)."""
        self.allowed = False
        self.code = code
        self.status, self.message = REASONS.get(code, (403, 'Request refused.'))
        return False


def normalize_sha256(value):
    """A certificate fingerprint as bare uppercase hex, or '' if it is not one."""
    cleaned = ''.join(ch for ch in (value or '') if ch not in ' :')
    if cleaned and all(ch in '0123456789abcdefABCDEF' for ch in cleaned):
        return cleaned.upper()
    return ''


def _truthy(raw):
    return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _body_sha256(request):
    """SHA-256 of the raw body, or of '' when it cannot be read."""
    try:
        body = request.body or b''
    except Exception:
        body = b''
    return hashlib.sha256(body).hexdigest()


def _claims(request):
    return {
        'platform': (request.META.get(HEADER_PLATFORM) or '').strip().lower(),
        'package_name': (request.META.get(HEADER_PACKAGE) or '').strip(),
        'installer': (request.META.get(HEADER_INSTALLER) or '').strip(),
        'signature_sha256': normalize_sha256(request.META.get(HEADER_SIGNATURE_SHA256)),
        'build_identifier': (request.META.get(HEADER_BUILD) or '').strip(),
        'device_model': (request.META.get(HEADER_DEVICE_MODEL) or '').strip(),
        'os_version': (request.META.get(HEADER_OS_VERSION) or '').strip(),
        'app_slug': (request.META.get(HEADER_APP_SLUG) or '').strip(),
    }


def _check_signature(request, app, result):
    """HMAC check: freshness, nonce, then the digest itself."""
    secret = (app.request_signing_secret or '').strip()
    if not secret:
        return result.refuse('signature_not_configured')

    sent = (request.META.get(HEADER_SIGNATURE) or '').strip().lower()
    stamp_raw = (request.META.get(HEADER_TIMESTAMP) or '').strip()
    nonce = (request.META.get(HEADER_NONCE) or '').strip()
    if not sent or not stamp_raw or not nonce:
        return result.refuse('missing_request_signature')

    try:
        stamp = int(stamp_raw)
    except (TypeError, ValueError):
        return result.refuse('invalid_request_signature')
    if abs(time.time() - stamp) > MAX_CLOCK_SKEW_SECONDS:
        return result.refuse('stale_request')

    if not 8 <= len(nonce) <= 128:
        return result.refuse('invalid_request_signature')
    # cache.add() is atomic on the backends we run, so the first request to
    # claim a nonce wins and every repeat inside the window is a replay.
    if not cache.add(f'acv:nonce:{app.pk}:{nonce}', 1, MAX_CLOCK_SKEW_SECONDS * 2):
        return result.refuse('replayed_request')

    canonical = '\n'.join([
        (request.method or '').upper(),
        request.path or '',
        stamp_raw,
        nonce,
        _body_sha256(request),
    ])
    expected = hmac.new(secret.encode('utf-8'), canonical.encode('utf-8'),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sent):
        return result.refuse('invalid_request_signature')
    return True


def validate_app_request(request, app):
    """Run the whole chain for one request. Never raises, never blocks."""
    result = ValidationResult(claims=_claims(request))
    try:
        if not _run_checks(request, app, result):
            return result
    except Exception:
        # A bug here must not take the API down: log it and let the request
        # through, because the shared credential and per-user token already
        # passed and this layer is an extra, not the lock.
        logger.exception('client validation failed unexpectedly; allowing request')
    return result


def _run_checks(request, app, result):
    """The chain itself. Returns False at the first refusal."""
    claims = result.claims

    # 1. A request that names a different app than its credentials belong to is
    #    either a copy-paste mistake or someone reusing another build's key.
    if claims['app_slug'] and app.slug and claims['app_slug'] != app.slug:
        return result.refuse('app_identity_mismatch')

    # 2. Platform. A non-Android client is out whatever else it claims.
    if app.require_android_platform:
        if not claims['platform']:
            return result.refuse('missing_platform_header')
        if claims['platform'] != 'android':
            return result.refuse('platform_not_android')

    # 3. Browser fingerprints. The app is OkHttp and sends none of these.
    if any(request.META.get(name) for name in BROWSER_ONLY_HEADERS):
        return result.refuse('browser_client')

    # 4. Package name allow-list.
    allowed_packages = app.allowed_package_list
    if allowed_packages:
        if not claims['package_name']:
            return result.refuse('missing_package_header')
        if claims['package_name'] not in allowed_packages:
            return result.refuse('package_mismatch')

    # 5. Installation source allow-list — this is what separates a Play install
    #    or an official APK from a sideload of an unknown file.
    allowed_installers = app.allowed_installer_list
    if allowed_installers:
        if not claims['installer']:
            return result.refuse('missing_installer_header')
        if claims['installer'] not in allowed_installers:
            return result.refuse('installer_not_allowed')

    # 6. Signing certificate. Compared as bare uppercase hex so the app may
    #    send the fingerprint with or without colons.
    allowed_signatures = app.allowed_signature_list
    if allowed_signatures:
        if not claims['signature_sha256']:
            return result.refuse('missing_signature_header')
        if claims['signature_sha256'] not in allowed_signatures:
            return result.refuse('signature_mismatch')

    # 7. The client's own tamper verdict. A modified build is refused before it
    #    gets to make any other claim count.
    if _truthy(request.META.get(HEADER_TAMPERED)):
        return result.refuse('tamper_detected')

    # 8. Device hygiene.
    if app.block_debug_builds and _truthy(request.META.get(HEADER_DEBUG)):
        return result.refuse('debug_build')
    if app.block_emulators and _truthy(request.META.get(HEADER_EMULATOR)):
        return result.refuse('emulator_detected')
    if app.block_rooted_devices and _truthy(request.META.get(HEADER_ROOTED)):
        return result.refuse('rooted_device')

    # 9. Optional HMAC request signing, last because it is the most expensive.
    if app.require_request_signature:
        return _check_signature(request, app, result)

    return True


def _record(request, app, result, strict, would_block):
    """Write the decision to the log and to the security logger.

    Refusals are always kept. Allows are kept only when strict mode is on for
    the app, so a per-request table does not grow from traffic that is already
    known to be fine.
    """
    code = result.code or 'ok'
    # "decision" is what the server actually did, so a would-be refusal in
    # observe mode is recorded as an allow carrying would_block — which is what
    # makes the trial run readable: the row shows both what happened and what
    # would have happened had enforcement been on.
    blocked = would_block and strict
    line = 'ACV %s app=%s path=%s reason=%s ip=%s' % (
        'block' if blocked else 'allow', app.slug, request.path, code,
        request.META.get('REMOTE_ADDR', ''))
    if blocked:
        logger.warning(line)
    elif strict:
        logger.info(line)
    else:
        logger.debug(line)

    if not would_block and not strict:
        return
    try:
        from .models import AppClientValidationLog

        claims = result.claims
        try:
            details = json.dumps({
                'claims': claims,
                'strict': strict,
                'user_agent': request.META.get('HTTP_USER_AGENT', '')[:200],
            })
        except (TypeError, ValueError):
            details = ''
        AppClientValidationLog.objects.create(
            android_app=app,
            decision='reject' if blocked else 'allow',
            reason_code='' if result.allowed else result.code,
            reason=result.message,
            path=request.path[:255],
            method=(request.method or '')[:10],
            ip_address=(request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                        or request.META.get('REMOTE_ADDR', ''))[:64],
            user_agent=(request.META.get('HTTP_USER_AGENT', '') or '')[:300],
            platform=claims.get('platform', '')[:32],
            package_name=claims.get('package_name', '')[:255],
            installer=claims.get('installer', '')[:255],
            signature_sha256=claims.get('signature_sha256', '')[:128],
            build_identifier=claims.get('build_identifier', '')[:120],
            device_model=claims.get('device_model', '')[:120],
            os_version=claims.get('os_version', '')[:40],
            would_block=would_block,
            strict=strict,
            details=details,
        )
    except Exception:
        # Logging must never be the reason an API call fails.
        logger.exception('could not write the client-validation log row')


def enforce_app_client(request, app):
    """Check, log, and return a refusal response only when strict mode is on.

    Called from ``_validate_android_auth`` so every Android-facing user endpoint
    gets the same treatment from one place. Returns ``None`` when the request
    may continue.
    """
    strict = bool(getattr(app, 'strict_client_validation', False))
    result = validate_app_request(request, app)
    would_block = not result.allowed
    _record(request, app, result, strict, would_block)
    if not would_block or not strict:
        return None

    from django.http import JsonResponse
    return JsonResponse({
        'status': 'error',
        'code': result.code,
        'message': result.message,
    }, status=result.status)


def expose_integrity_verdict(app, request):
    """Where a Play Integrity verdict would be checked, when the app sends one.

    Left deliberately unimplemented rather than faked: real attestation has to
    come from Google's Play Integrity API (Google signs a verdict with the
    package name, certificate digest and app-recognition verdict inside it), and
    no header a client can write is a substitute. When the app is updated to
    request a token and forward it here, this is the single place that has to
    change: verify the token with Google, then compare the package name and
    certificate digest in the verdict against the app's allow-lists.
    """
    return None