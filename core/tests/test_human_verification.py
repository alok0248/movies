"""Layer 1: the Cloudflare Turnstile human gate.

Two halves are covered.

* The **middleware**: an unverified browser is redirected (GET) or refused
  (POST), the exempt routes and the Android app pass untouched, Google's
  crawler is let in because indexing matters, and every other crawler is not.
* The **/verify/ endpoint**: the widget is rendered with the site key, the token
  is checked server-side against a *mocked* Cloudflare, the hostname on the
  answer is compared with this site, and every failure mode (timeout, network
  error, refused token, wrong host, missing token) re-renders the page with a
  message instead of raising.

No test ever reaches Cloudflare: TURNSTILE_VERIFY_URL is pointed at a fake
address and `requests.post` is patched.
"""
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from core import human_verification as hv

GATE_ON = dict(
    TURNSTILE_SITE_KEY='test-site-key',
    TURNSTILE_SECRET_KEY='test-secret-key',
    TURNSTILE_VERIFY_URL='https://turnstile.test/siteverify',
    TURNSTILE_TIMEOUT_SECONDS=5,
)
GATE_OFF = dict(
    TURNSTILE_SITE_KEY='YOUR_SITE_KEY',
    TURNSTILE_SECRET_KEY='YOUR_SECRET_KEY',
)


class FakeResponse:
    """The bits of requests.Response the code touches."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class SafeNextTest(TestCase):
    """The ``next`` parameter must never send a visitor off-site."""

    def test_a_local_path_is_kept(self):
        self.assertEqual('/movies/?page=2', hv.safe_next('/movies/?page=2', 'newmovies.linkpc.net'))

    def test_an_absolute_url_on_this_host_is_reduced_to_its_path(self):
        self.assertEqual('/series/', hv.safe_next('https://newmovies.linkpc.net/series/', 'newmovies.linkpc.net'))

    def test_another_host_is_replaced(self):
        for value in ['https://evil.example/x', '//evil.example/x', '/\\evil.example', 'http://evil.example']:
            with self.subTest(value=value):
                self.assertEqual('/', hv.safe_next(value, 'newmovies.linkpc.net'))

    def test_a_relative_path_without_a_leading_slash_is_replaced(self):
        self.assertEqual('/', hv.safe_next('movies/', 'newmovies.linkpc.net'))

    def test_javascript_and_control_characters_are_replaced(self):
        for value in ['javascript:alert(1)', '/ok\r\nSet-Cookie: x=1', '']:
            with self.subTest(value=value):
                self.assertEqual('/', hv.safe_next(value, 'newmovies.linkpc.net'))


class SessionMarkTest(TestCase):
    def setUp(self):
        self.client = Client()

    def _request(self):
        self.client.get('/security-status/')
        return self.client.session

    def test_an_unmarked_session_is_not_human(self):
        self.client.get('/security-status/')
        self.assertFalse(hv.is_human(_SessionRequest(self.client.session)))

    @override_settings(**GATE_ON)
    def test_marking_a_session_makes_it_human(self):
        self.client.get('/security-status/')
        session = self.client.session
        hv.mark_human(_SessionRequest(session))
        self.assertTrue(hv.is_human(_SessionRequest(session)))

    def test_an_expired_mark_is_dropped(self):
        self.client.get('/security-status/')
        session = self.client.session
        session[hv.SESSION_HUMAN_KEY] = True
        session[hv.SESSION_HUMAN_AT_KEY] = 1  # 1970: far past the 24h window
        session.save()
        self.assertFalse(hv.is_human(_SessionRequest(session)))
        self.assertNotIn(hv.SESSION_HUMAN_KEY, session)


class _SessionRequest:
    """The two attributes is_human/mark_human read."""

    def __init__(self, session):
        self.session = session
        self.path = '/'
        self.META = {}


@override_settings(**GATE_ON)
class MiddlewareTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_an_unverified_browser_is_redirected_with_the_original_path(self):
        response = self.client.get('/movies/')
        self.assertEqual(302, response.status_code)
        self.assertIn('/verify/?next=%2Fmovies%2F', response['Location'])

    def test_a_head_request_is_redirected_too(self):
        response = self.client.head('/movies/')
        self.assertEqual(302, response.status_code)

    def test_an_unverified_post_is_refused(self):
        response = self.client.post('/movies/')
        self.assertEqual(403, response.status_code)

    def test_an_unverified_ajax_post_gets_json(self):
        response = self.client.post('/ajax/login/', {}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(403, response.status_code)
        self.assertEqual('human_verification_required', response.json()['code'])

    def test_the_challenge_page_itself_is_open(self):
        self.assertEqual(200, self.client.get('/verify/').status_code)

    def test_the_status_page_is_open(self):
        self.assertEqual(200, self.client.get('/security-status/').status_code)

    def test_the_admin_and_api_are_never_gated(self):
        # /admin/ may redirect to its own login; what matters is that the gate
        # never sends any of these to /verify/.
        for path in ['/admin/', '/api/messages/', '/apps/download/3/']:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn('/verify/', response.get('Location', ''))

    def test_static_and_media_are_never_gated(self):
        for path in ['/static/css/x.css', '/media/anything.png']:
            with self.subTest(path=path):
                self.assertNotEqual(302, self.client.get(path).status_code)

    def test_google_is_let_through(self):
        response = self.client.get('/movies/', HTTP_USER_AGENT='Mozilla/5.0 (compatible; Googlebot/2.1)')
        self.assertEqual(200, response.status_code)

    def test_any_other_crawler_is_not(self):
        for agent in ['Mozilla/5.0 (compatible; bingbot/2.0)', 'GPTBot/1.0', 'ClaudeBot/1.0']:
            with self.subTest(agent=agent):
                response = self.client.get('/movies/', HTTP_USER_AGENT=agent)
                self.assertEqual(302, response.status_code)

    def test_the_android_app_is_let_through(self):
        response = self.client.get('/movies/', HTTP_USER_AGENT='okhttp/4.12.0')
        self.assertEqual(200, response.status_code)

    def test_the_app_header_is_let_through(self):
        response = self.client.get('/movies/', HTTP_X_ANDROID_APP='cinestream')
        self.assertEqual(200, response.status_code)

    def test_a_verified_session_browses_normally(self):
        self.client.get('/security-status/')
        session = self.client.session
        session[hv.SESSION_HUMAN_KEY] = True
        session[hv.SESSION_HUMAN_AT_KEY] = __import__('time').time()
        session.save()
        self.assertEqual(200, self.client.get('/movies/').status_code)


@override_settings(**GATE_OFF)
class UnconfiguredGateTest(TestCase):
    """No keys means no gate: a deploy before the keys exist must not lock out."""

    def test_pages_are_served_without_a_challenge(self):
        self.assertEqual(200, Client().get('/movies/').status_code)

    def test_the_verify_page_lets_the_visitor_straight_through(self):
        client = Client()
        response = client.get('/verify/?next=/movies/')
        self.assertEqual(302, response.status_code)
        self.assertEqual('/movies/', response['Location'])


class VerifyViewTest(TestCase):
    """The /verify/ endpoint, with Cloudflare mocked."""

    def setUp(self):
        self.client = Client()
        self.verify = override_settings(**GATE_ON)
        self.verify.enable()
        self.addCleanup(self.verify.disable)

    # ------------------------------------------------------------------ render
    def test_the_page_renders_the_widget_and_a_csrf_protected_form(self):
        response = self.client.get('/verify/?next=/movies/')
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'cf-turnstile')
        self.assertContains(response, 'data-sitekey="test-site-key"')
        self.assertContains(response, 'challenges.cloudflare.com/turnstile/v0/api.js')
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_the_next_value_is_carried_into_the_form_safely(self):
        response = self.client.get('/verify/?next=https://evil.example/x')
        self.assertContains(response, 'name="next" value="/"')

    # ------------------------------------------------------------------ success
    @patch('core.human_verification.requests.post')
    def test_a_valid_token_marks_the_session_and_redirects(self, post):
        post.return_value = FakeResponse({'success': True, 'hostname': 'testserver'})
        response = self.client.post('/verify/', {
            'cf-turnstile-response': 'good-token', 'next': '/movies/',
        })
        self.assertEqual(302, response.status_code)
        self.assertEqual('/movies/', response['Location'])
        self.assertTrue(hv.is_human(_SessionRequest(self.client.session)))

    @patch('core.human_verification.requests.post')
    def test_the_siteverify_call_carries_secret_response_and_remoteip(self, post):
        post.return_value = FakeResponse({'success': True, 'hostname': 'testserver'})
        self.client.post('/verify/', {'cf-turnstile-response': 'abc123', 'next': '/'},
                         REMOTE_ADDR='203.0.113.9')
        _args, kwargs = post.call_args
        payload = kwargs['data']
        self.assertEqual('test-secret-key', payload['secret'])
        self.assertEqual('abc123', payload['response'])
        self.assertEqual('203.0.113.9', payload['remoteip'])
        self.assertEqual(5, kwargs['timeout'])

    # ---------------------------------------------------------------- failures
    @patch('core.human_verification.requests.post')
    def test_a_missing_token_shows_the_page_again(self, post):
        response = self.client.post('/verify/', {'next': '/'})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'missing')
        post.assert_not_called()

    @patch('core.human_verification.requests.post')
    def test_a_refused_token_shows_a_retryable_message(self, post):
        post.return_value = FakeResponse({'success': False, 'error-codes': ['invalid-input-response']})
        response = self.client.post('/verify/', {'cf-turnstile-response': 'spent', 'next': '/'})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'not solved')
        self.assertFalse(hv.is_human(_SessionRequest(self.client.session)))

    @patch('core.human_verification.requests.post')
    def test_a_token_for_another_hostname_is_refused(self, post):
        post.return_value = FakeResponse({'success': True, 'hostname': 'someone-elses.site'})
        response = self.client.post('/verify/', {'cf-turnstile-response': 'x', 'next': '/'})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'different site')
        self.assertFalse(hv.is_human(_SessionRequest(self.client.session)))

    @patch('core.human_verification.requests.post')
    def test_a_timeout_is_handled_gracefully(self, post):
        import requests
        post.side_effect = requests.Timeout()
        response = self.client.post('/verify/', {'cf-turnstile-response': 'x', 'next': '/'})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'did not answer in time')

    @patch('core.human_verification.requests.post')
    def test_a_network_error_is_handled_gracefully(self, post):
        import requests
        post.side_effect = requests.ConnectionError('no route')
        response = self.client.post('/verify/', {'cf-turnstile-response': 'x', 'next': '/'})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'could not be reached')

    @patch('core.human_verification.requests.post')
    def test_an_unreadable_reply_is_handled_gracefully(self, post):
        post.return_value = FakeResponse(ValueError('not json'))
        response = self.client.post('/verify/', {'cf-turnstile-response': 'x', 'next': '/'})
        self.assertEqual(200, response.status_code)
        self.assertContains(response, 'unreadable')

    @patch('core.human_verification.requests.post')
    def test_an_open_redirect_next_is_neutralised_on_success(self, post):
        post.return_value = FakeResponse({'success': True, 'hostname': 'testserver'})
        response = self.client.post('/verify/', {
            'cf-turnstile-response': 'x', 'next': 'https://evil.example/steal',
        })
        self.assertEqual(302, response.status_code)
        self.assertEqual('/', response['Location'])