"""Ask the running site the questions a deploy must never break.

    ./venv/bin/python manage.py smoke_test
    ./venv/bin/python manage.py smoke_test --base-url http://127.0.0.1:8001   # staging

Runs after a restart (and against staging *before* one) so a broken release is
caught by a machine instead of by the first visitor. Every check is read-only and
uses the public surface — the pages a person opens first, the admin entry point
and the Android endpoints the installed apps call — so a pass means "the site is
serving", not "the tests pass".

The app credential defaults to the shared install credential; override it with
``--app-user/--app-pass`` or ``SMOKE_APP_USER`` / ``SMOKE_APP_PASS``.

Exit code is 1 when any check fails, which is what lets the deploy script stop
and shout instead of reporting success.
"""
import base64
import json
import os
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand

DEFAULT_USER = os.environ.get('SMOKE_APP_USER', 'testing')
DEFAULT_PASS = os.environ.get('SMOKE_APP_PASS', 'testing')
PROBE_EMAIL = 'smoke-test@example.invalid'

# name, path, expected statuses, needs app credential
CHECKS = [
    ('home page', '/', {200, 302}, False),
    ('browse page', '/movies/', {200, 302}, False),
    ('privacy page', '/privacy/', {200, 302}, False),
    ('admin entry', '/admin/', {200, 302}, False),
    ('messages without email', '/api/messages/', {400}, True),
    ('messages with email', '/api/messages/?email=' + PROBE_EMAIL, {200}, True),
    ('notifications with email', '/api/notifications/?email=' + PROBE_EMAIL, {200}, True),
]


class Command(BaseCommand):
    help = 'Check that the running site still serves its pages and app endpoints.'

    def add_arguments(self, parser):
        parser.add_argument('--base-url', default='http://127.0.0.1:8000',
                            help='Where to send the checks (default http://127.0.0.1:8000).')
        parser.add_argument('--host', default=None,
                            help='Host header to send (default: the host in --base-url).')
        parser.add_argument('--app-user', default=DEFAULT_USER, help='Basic-auth user for app endpoints.')
        parser.add_argument('--app-pass', default=DEFAULT_PASS, help='Basic-auth password for app endpoints.')
        parser.add_argument('--timeout', type=float, default=20.0, help='Per-request timeout in seconds.')
        parser.add_argument('--json', action='store_true', help='Print a JSON summary instead of lines.')

    def _fetch(self, url, host, credentials, timeout):
        request = urllib.request.Request(url, method='GET')
        if host:
            request.add_header('Host', host)
        request.add_header('User-Agent', 'CinestreamSmokeTest/1.0')
        if credentials:
            request.add_header('Authorization', 'Basic ' + base64.b64encode(
                credentials.encode('utf-8')).decode('ascii'))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read(4000)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(4000)
        except Exception as exc:
            return None, str(exc).encode('utf-8', 'replace')

    def _post_json(self, url, host, credentials, payload, timeout):
        """POST JSON, following a redirect without dropping the body.

        The site redirects http to https, and urllib's default handler turns a
        redirected POST into a GET - which hits 405 and would report the app
        endpoint as broken when it is only the scheme that moved.
        """
        class _KeepPost(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                new = super().redirect_request(req, fp, code, msg, headers, newurl)
                if new is None:
                    return None
                if code in (301, 302, 303, 307, 308) and req.data is not None:
                    new.method = 'POST'
                    new.data = req.data
                    for header in ('Content-Type', 'Authorization', 'User-Agent'):
                        value = req.get_header(header)
                        if value:
                            new.add_unredirected_header(header, value)
                return new

        body = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(url, data=body, method='POST')
        if host:
            request.add_header('Host', host)
        request.add_header('Content-Type', 'application/json')
        request.add_header('User-Agent', 'CinestreamSmokeTest/1.0')
        if credentials:
            request.add_header('Authorization', 'Basic ' + base64.b64encode(
                credentials.encode('utf-8')).decode('ascii'))
        opener = urllib.request.build_opener(_KeepPost)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(8000)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(8000)
        except Exception as exc:
            return None, str(exc).encode('utf-8', 'replace')

    def handle(self, *args, **options):
        base = options['base_url'].rstrip('/')
        host = options['host'] or base.split('//')[-1].split('/')[0]
        credentials = '%s:%s' % (options['app_user'], options['app_pass'])
        results, failures = [], 0

        for name, path, expected, needs_auth in CHECKS:
            status, body = self._fetch(base + path, host,
                                       credentials if needs_auth else None,
                                       options['timeout'])
            ok = status in expected
            detail = 'HTTP %s' % (status if status is not None else 'no response')
            if status is None:
                detail = body[:160].decode('utf-8', 'replace')
            elif not ok:
                head = body[:160].decode('utf-8', 'replace').replace('\n', ' ')
                detail += ' (wanted %s) %s' % ('/'.join(str(s) for s in sorted(expected)), head)
            results.append({'check': name, 'path': path, 'status': status, 'ok': ok, 'detail': detail})
            if not ok:
                failures += 1

        # --- the app's own sync call -------------------------------------
        # Old builds depend on this response shape, so the smoke suite asserts
        # the keys they parse rather than only the status code.
        sync_path = '/api/user/sync/'
        status, body = self._post_json(base + sync_path, host, credentials,
                                       {'email': PROBE_EMAIL, 'userData': {}},
                                       options['timeout'])
        if status != 200:
            # Production answers plain http with a 301 to https; ask the public
            # origin directly the way a phone would.
            status, body = self._post_json('https://' + host + sync_path, None, credentials,
                                           {'email': PROBE_EMAIL, 'userData': {}},
                                           options['timeout'])
            sync_path += ' (https)'
        detail, ok = 'HTTP %s' % (status if status is not None else 'no response'), False
        if status is None:
            detail = body[:160].decode('utf-8', 'replace')
        elif status != 200:
            detail += ' (wanted 200) ' + body[:160].decode('utf-8', 'replace').replace('\n', ' ')
        else:
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
            missing = [k for k in ('status', 'subscription', 'userData', 'remoteConfig')
                       if k not in payload]
            ok = not missing and payload.get('status') == 'success'
            detail = ('all four keys present' if ok
                      else 'missing %s' % ', '.join(missing) if missing
                      else 'status=%r' % payload.get('status'))
        results.append({'check': 'app sync payload', 'path': sync_path,
                        'status': status, 'ok': ok, 'detail': detail})
        if not ok:
            failures += 1

        if options['json']:
            self.stdout.write(json.dumps({'base_url': base, 'ok': failures == 0,
                                          'failures': failures, 'checks': results}, indent=2))
        else:
            for row in results:
                mark = self.style.SUCCESS('PASS') if row['ok'] else self.style.ERROR('FAIL')
                self.stdout.write('  %s  %-26s %s — %s' % (mark, row['check'], row['path'], row['detail']))
            if failures:
                self.stderr.write(self.style.ERROR(
                    'smoke test failed: %d of %d checks' % (failures, len(results))))
                raise SystemExit(1)
            self.stdout.write(self.style.SUCCESS(
                'smoke test passed: %d checks against %s' % (len(results), base)))
