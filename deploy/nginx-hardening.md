# nginx hardening for newmovies.linkpc.net — instructions, not a config file

Five hand edits to `/etc/nginx/sites-enabled/movies` on `151.145.57.106`, one at a time, with
`sudo nginx -t` after each. That file (119 lines) is this site's entire nginx config: nginx loads
it through `include /etc/nginx/sites-enabled/*;` (`nginx.conf:61`), and it is the only file in
that directory.

**Never copy, `include` or paste this file as a whole.** The prose between the blocks is not nginx
syntax, so an include aborts `nginx -t`.

**Which block.** The file holds two `server` blocks, lines 14–65 and 68–119, and both contain
`listen 443 ssl;`, `location /media/`, `location /api/` and `location /`, so those lines cannot
tell them apart. Every snippet except 1 and 2 belongs inside the **second** block — the one whose
`server_name newmovies.linkpc.net;` is line 71 — not the first, which has
`listen 80 default_server;` (line 15) and `server_name _;` (line 17). Snippet 1 goes at the top of
the file, outside both blocks; snippet 2 is a new block of its own.
Check: `sudo grep -n server_name /etc/nginx/sites-enabled/movies` → `17:    server_name _;` and
`71:    server_name newmovies.linkpc.net;`.

Every line number below is the current one. Snippet 1 inserts two lines at the top, so apply it
first and add 2 to every later number.

## Apply

```
sudo cp -a /etc/nginx/sites-enabled/movies /etc/nginx/movies.bak-$(date +%F)
sudoedit /etc/nginx/sites-enabled/movies     # merge ONE snippet
sudo nginx -t                                # must say "test is successful"
sudo systemctl reload nginx                  # reload, not restart: no downtime
```

The backup goes in `/etc/nginx/`, **never** in `sites-enabled/`: `nginx.conf:61` is
`include /etc/nginx/sites-enabled/*;`, so a backup file there is parsed as a second config, and its
duplicate `listen 443 ssl default_server;` (line 16) plus `server_name _;` (line 17) make
`nginx -t` fail.

`server/server_admin.bat` does not back this file up. Option `[4] Nginx Status & Logs` runs
`sudo nginx -t` and prints the file — read-only, and the quickest way to test a merge. Option
`[2] Pull GitHub Update` is the app deploy, and it restarts nginx without testing the config
first, so merge and validate here **before** running it.

## Roll back

```
sudo cp -a "$(ls -t /etc/nginx/movies.bak-* | head -1)" /etc/nginx/sites-enabled/movies
sudo nginx -t && sudo systemctl reload nginx
```

---

## 1. `http` context — a rate-limit zone for the proxy endpoints

Both lines go at the top of the file, directly under the existing `limit_req_zone` on line 4
(`limit_conn_zone` is line 3). They are legal only in `http`, i.e. outside both `server` blocks.

The existing `ratelimit` zone (line 4, 30r/s) is referenced only by the two `location /api/`
blocks — lines 47 and 101, nowhere else on the box. Every `/proxy/` request makes gunicorn fetch an
upstream stream, so it gets its own bucket — sized by measurement to sit **above** the app's own
ceiling, not below it (next subsection). `proxy_ratelimit` is a new name, so it cannot collide
with `ratelimit`.

```nginx
# top of the file, directly under the limit_req_zone line (line 4)
limit_req_zone $binary_remote_addr zone=proxy_ratelimit:10m rate=60r/s;
limit_req_status 429;
```

**Exactly once.** A second zone with the same name, or a second `limit_req_status` in `http`, is a
"duplicate directive" error from `nginx -t`.

Check: `sudo nginx -T | grep -c 'zone=proxy_ratelimit'` → `1`.

### Why `rate=60r/s` and `burst=120` — measured 2026-11-13, not estimated

An earlier draft of this document set `rate=10r/s` / `burst=30` as an estimate. That is **5× below**
the app's own `/proxy/` budget, so nginx, not the app, would have decided when a client had asked
for too much — the mid-playback `429` this measurement was asked about. Every access log the box
retains (15 files: `access.log`, `access.log.1`, `access.log.2.gz` … `access.log.14.gz`,
**727,447 lines, 2026-10-30 00:02:51 → 2026-11-13 11:57:43 UTC**) says the estimate cannot be
supported in either direction:

| | `/proxy/` | `/api/` |
|---|---|---|
| requests in the window | 41 | 33,929 |
| distinct client IPs | 2 | 404 |
| per-IP peak in the busiest second (p50 / p90 / p99 / max) | 1 / 2 / 2 / **2 r/s** | 1 / 2 / 6 / **14 r/s** |
| per-IP peak in the busiest minute (p50 / p90 / p99 / max) | 1 / 13 / 13 / 13 | 2 / 6 / 25 / 84 |
| busiest (IP, second) bucket | 2 | 14 |
| requests a `limit_req` has ever rejected | 0 | 0 |

**Why that window is 15 days, not 30:** `/etc/logrotate.d/nginx` is `daily` with `rotate 14` and no
`maxage`/`size` override, so the oldest log on the box is the 15th day back. Thirty days of data do
not exist to measure — the table above is all of it.

**`/proxy/` has no traffic to size a limit from, and no video byte has ever passed through nginx.**
All 41 requests are SSRF and traversal probes: 39 from `152.59.143.58` (`curl/8.22.0`) and 2 from
`152.59.143.146`, at `/proxy/api.speedracelight.comapi/public/init` (14),
`/proxy/example.com/` (7), `/proxy/api.speedracelight.comapi/search/result` (5),
`/proxy/169.254.169.254/latest/meta-data/`, `/proxy/127.0.0.1:8000/`, `/proxy/[::1]/`,
`/proxy/%2E%2E%2F%2E%2E%2Fetc%2Fpasswd` and similar, answered `403` (20), `400` (9), `200` (8),
`404` (3), `502` (1). No legitimate client has used the route. The players say why:
`templates/core/movie_detail.html:1383` hands a `.m3u8` upstream URL to `window.openOwnTab()`, so
the manifest and its segments are fetched by the browser **from the upstream host**, never from
this box, and `static/sw-proxy.js` intercepts only `/proxy/` URLs, which these flows never issue.
A seek burst therefore cannot be measured here; it has to be sized from the code:

- **`rate=60r/s` is 2× the app's own ceiling.** `core/proxy_guard.py:56`
  (`PROXY_RATE_LIMIT_PER_MINUTE = 3000`) is 3000/min per IP = 50 r/s, so `10r/s` made nginx
  stricter than the app it sits in front of. At `60r/s` nginx still absorbs a flood, but the app's
  per-minute budget stays the thing that refuses a client: nginx cannot pre-empt it. The measured
  `/proxy/` peak (2 r/s) is 30× under it and the busiest legitimate client anywhere on the box
  (12 r/s, `okhttp/4.12.0`) is 5× under it.
- **`burst=120` is 2 s of that rate, and covers a seek refill.** `hls.js` refills after a seek with
  `startFragPrefetch: true`, `maxBufferLength: 1800`, `maxMaxBufferLength: 7200`,
  `maxBufferSize: 1 GB` (`player/player_frame.html:164`, `templates/cineplayer/player.html:762`,
  `static/js/player-core.js:867`), so a seek can queue far more fragments than the library's
  default — but the browser, not the queue, sets the arrival rate: this box speaks HTTP/1.1 only
  (`listen 443 ssl;`, line 70, and `grep -rn http2 /etc/nginx/` → no match), so a client holds ~6
  connections per host and the wave arrives at ~6 r/s. 120 absorbs ~20 s of that with no
  rejection. It is also 8× the busiest per-IP second measured anywhere in the window (`14`,
  `45.61.178.105`, a scanner on `/api/`).
- **`nodelay` stays on.** The bucket is here to stop a flood, not to pace a burst that `hls.js` is
  already pacing itself.

**`/api/` is left exactly as it is — `30r/s`, `burst=20`, lines 47 and 101 — because the measurement
validates it.** The busiest per-IP second in 15 days was 14 r/s and the busiest legitimate one
12 r/s, both far under 30 r/s. And no `/api/` request has ever been rejected: a `limit_req`
rejection is a `503` on this box (there is no `limit_req_status` anywhere in `/etc/nginx/` —
`grep -rn limit_req_status /etc/nginx/` → no match — so the directive's default applies), and all
1,704 `503`s in the window belong to `limit_conn connlimit 50` (line 80) firing at a scanner:
1,337 of them from `45.61.178.105` on 08/Nov, and **none on `/api/`, `/cineplayer/` or
`/ajax/`**. The 336 `429`s are the app's own `/api/user/sync` budget, not this limiter — 15/min per
account and 60/min per address (`core/models.py:1098`, `core/views.py:11297`), with 325 of the 336
from one client on 09/Nov.

Reproduce all of it on the box — read-only, no root needed (`ubuntu` is in `adm`; the logs are
`www-data:adm 0640`):

```
cd /var/log/nginx

# the file set, and the real window
zcat -f access.log access.log.1 access.log.*.gz | wc -l
zcat -f $(ls -v access.log.*.gz | tail -1) | head -1 | cut -d' ' -f4    # oldest line
zcat -f access.log | tail -1 | cut -d' ' -f4                           # newest line

# per-IP peak requests in any one second, /api/ ($7 is the request path in this format)
zcat -f access.log access.log.1 access.log.*.gz |
  awk '$7 ~ /^\/api\// {split($4,t,":"); c[$1" "t[2]":"t[3]":"t[4]]++}
       END{for(k in c){split(k,a," "); if(c[k]>m[a[1]]) m[a[1]]=c[k]} for(ip in m) print m[ip], ip}' |
  sort -rn | head -5

# swap /^\/api\// for /^\/proxy\// for the other column; this prints every /proxy/ request
zcat -f access.log access.log.1 access.log.*.gz | awk '$7 ~ /^\/proxy\//'

# media bytes: scanner probes only, no manifest and no segment
zcat -f access.log access.log.1 access.log.*.gz | awk '$7 ~ /\.(m3u8|m4s|mp4|ts)$/ {print $7}' | sort | uniq -c
```

The run of 2026-11-13 printed, in order: `727447`; `[30/Oct/2026:00:02:51`;
`[13/Nov/2026:11:57:43`; `14 45.61.178.105`, `12 80.2.180.225`, `12 152.59.143.58`,
`7 185.213.154.227`, `6 49.207.192.45`; the 41 probes above; and 7 media-extension requests, every
one of them a scanner probe (`/api/index.ts` ×3, `/api/aws/s3.ts`, `/config.ts`, `/environment.ts`,
`/nuxt.config.ts`) — no `.m3u8` and no segment request at all.

## 2. A new port-80 `server` block, and one deleted line

Plain http still serves everything: `curl -s -o /dev/null -w '%{http_code}\n'
http://newmovies.linkpc.net/` → `200`, and the same request for
`http://newmovies.linkpc.net/static/js/player-core.js` → `200`. Django's `SECURE_SSL_REDIRECT`
(`movie_portal/settings.py:263`) only sees requests that reach Django; nginx answers `/static/`
(line 86) and `/media/` (line 93) from disk, so those would stay plaintext even after the app
change is deployed. Port 80 in nginx is the only place that can redirect them.

Add this as **its own top-level `server` block after line 119** (a `server` inside a `server` is
invalid), then **delete the bare `listen 80;` line in the `newmovies.linkpc.net` block**: exactly
one block may own port 80 for that name.

```nginx
# new top-level block, after the newmovies.linkpc.net block
server {
    listen 80;
    server_name newmovies.linkpc.net;

    # ACME http-01 challenge files; see the certificate note below
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
```

Checks: `curl -sI http://newmovies.linkpc.net/ | head -1` → `HTTP/1.1 301 Moved Permanently`; and
`curl -sI http://newmovies.linkpc.net/.well-known/acme-challenge/x | grep -ci '^location:'` → `0`
(a `1` means that path is redirected too, i.e. the exception is missing or misplaced). Both are
`200`/`0` today, so a still-`200` root means the old `listen 80;` is still there:
`sudo grep -n '^[[:space:]]*listen 80;' /etc/nginx/sites-enabled/movies` → the single line
`69:    listen 80;` (the first block's is `listen 80 default_server;`, line 15).

The ACME exception is not decoration. The certificate that line 73 serves is Let's Encrypt-issued
and expires **2026-11-19**:
`openssl x509 -in /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer -noout -enddate` →
`notAfter=Nov 19 02:11:38 2026 GMT`, six days out. Renewal has to reach port 80 again, and it
cannot use `/var/www/html` if this location is not here. Today nothing renews it:
`ls /etc/letsencrypt/renewal` → `No such file or directory`, so `certbot renew --dry-run` has no
lineage to renew, and the cron entry `0 3 1 * * /usr/local/bin/ssl_auto_renew.sh` points at a
script that does not exist (`ls -l /usr/local/bin/ssl_auto_renew.sh`). Wiring renewal up is a
separate job from this block; keeping the path open is what this snippet owes it. That job is
written up in [`ssl-renewal.md`](ssl-renewal.md), and it is what uses `/var/www/html` below.

## 3. Site block — stop advertising the nginx version

One line, next to the `ssl_*` lines (73–77) of the `newmovies.linkpc.net` block:

```nginx
# inside the server_name newmovies.linkpc.net block
server_tokens off;
```

Today every response names the exact build to aim at:
`curl -sI https://newmovies.linkpc.net/ | grep -i '^server:'` → `Server: nginx/1.18.0 (Ubuntu)`.

Check after the reload: the same command → `Server: nginx`.

Scope: this covers the named host only. The first block (14–65) is the default for the bare IP and
keeps advertising the version — `curl -skI https://151.145.57.106/ | grep -i '^server:'` still →
`Server: nginx/1.18.0 (Ubuntu)`. Covering both means uncommenting the `# server_tokens off;` that
is already at `/etc/nginx/nginx.conf:21` instead of this line; that is `http` context, so it takes
the same `nginx -t` and reload, and either placement alone is enough.

HSTS is deliberately not added here. The app sends it: `SECURE_HSTS_SECONDS` is `15552000`
(`movie_portal/settings.py:271`) and `SECURE_PROXY_SSL_HEADER` (line 202) makes `is_secure()`
follow `X-Forwarded-Proto`, which `/etc/nginx/proxy_params` sets on its line 4 — an `add_header`
here would only duplicate the header. Reproduced locally against the app change (`DJANGO_ENV=prod`,
`HTTP_X_FORWARDED_PROTO=https`) → `Strict-Transport-Security: max-age=15552000`. Check once the app
is deployed: `curl -sI https://newmovies.linkpc.net/ | grep -ci '^strict-transport'` → `1`. It is
`0` today, because the server still runs the pre-change code.

## 4. Site block — stop nginx serving the APKs off disk

```nginx
# inside the server_name newmovies.linkpc.net block
location ^~ /media/android_apks/ {
    return 404;
    access_log off;
}
```

`^~` and the longer prefix both make this win over `location /media/` (line 93), so the order of
the two in the file does not matter. Today the whole 24 MB APK downloads by guessing the name:
`curl -s -o /dev/null -r 0-10 -w '%{http_code}\n'
https://newmovies.linkpc.net/media/android_apks/saavnMusic.apk` → `206` with
`Content-Range: bytes 0-10/24241505`.

Downloads belong to the app's own route, `/apps/download/<id>/` (`core/urls.py:129`), and that route
is the only one in the code — audited 2026-11-13: `grep -ril '/media/' templates static` → no
matches (`static/sw-proxy.js` intercepts and caches `/proxy/` URLs only), the app's update payload
builds the route (`core/views.py:5409`, `reverse('apk_download')`), the admin list links it
(`templates/core/android_app_list.html:70`), and the only row carrying an APK is `core_androidapp`
id 3 (`android_apks/saavnMusic.apk`; ids 1, 2 and 4 have an empty `apk_file`, and no `json_payload`
holds an APK or `/media/` URL). The admin form, `templates/core/android_app_form.html`, was the
only page that linked a disk path, and it did so twice: its "Current APK" link (line 55) built
`{{ android_app.apk_file.url }}`, and the field above it rendered Django's `ClearableFileInput`,
whose own "Currently:" link points at the same path. Both ship fixed with this snippet — the field
is now rendered by hand (line 61) with the stored name as plain text and a `apk_file-clear`
checkbox that still clears it (checked through the form: clear → field emptied, upload → file
saved), and the link is now `{% url 'apk_download' android_app.id %}` (line 65) like the list page.
Re-check after deploying: `grep -ril '/media/' templates static` → no matches, and open
`/admin-dashboard/android-apps/3/edit/` as staff and search the page for `/media/` → no match.

**Exactly once.** A second `location ^~ /media/android_apks/` in the same block is a
duplicate-location error.

Checks after the reload: the same `curl -r 0-10` → `404`. The download route still works:
`curl -sI https://newmovies.linkpc.net/apps/download/3/` → `200`, `Content-Length: 24241505`,
`Content-Disposition: attachment; filename="saavnMusic.apk"` (id 3 is the app that has an APK; ids
1, 2 and 4 have none and answer `404`). Use `-I` there, never `-r 0-10`: that route is Django's
`FileResponse`, which ignores ranges and sends the whole 24 MB with a plain `200`. The bare directory keeps its
trailing-slash redirect first — `curl -s -o /dev/null -w '%{http_code}\n'
https://newmovies.linkpc.net/media/android_apks` → `301` (that is its value today too), and its
target now `404`s: the fix working, not a miss. `/media/` holds nothing but `android_apks/`
(`ls /home/ubuntu/project/movies/media`), so no non-APK media file is left to re-test; the line 93
block is untouched.

## 5. Site block — throttle `/proxy/`, and keep long streams alive

Needs snippet 1: without the zone, `nginx -t` fails with "unknown limit_req_zone". The file has no
`/proxy/` location today (`sudo grep -c 'location /proxy/'
/etc/nginx/sites-enabled/movies` → `0`), so add one. `/proxy/` then stops matching `location /`
(108–118), and everything that block supplies has to be repeated — but three of its settings are
wrong for this route, so the block below is deliberately **not** a copy of `location /`:

- **`proxy_buffering off`, where `location /` sets `on` (line 115).** Neither location has a
  `proxy_cache`, so buffering buys no cache hit; all it does is let nginx absorb a slow client and
  release the gunicorn thread early. `/proxy/` relays remote video instead of Django's own small
  responses: the body is a `StreamingHttpResponse` (`core/views.py:8761`), the client's `Range`
  header is never forwarded (`headers` at `core/views.py:8660-8663` carries only
  User-Agent/Origin/Referer, and `guarded_get` adds nothing — `core/url_guard.py:202` forwards
  `**kwargs` to `requests` unchanged), and the route advertises `Accept-Ranges: bytes` regardless
  (`core/views.py:8763`), so a seek is a fresh whole-object stream from byte 0. `location /`'s buffers
  (16k + 4×32k = 144 KB, lines 116–117) push every byte above that, per in-flight request, into
  `/var/lib/nginx/proxy` on the 45 GB system disk (`df -h /` → 38 GB free; the path is nginx's
  compiled `--http-proxy-temp-path`). Buffering off keeps one buffer in memory and forwards bytes to
  the client as they arrive. The cost is explicit, not hidden: a gunicorn thread stays on the request
  for the client's whole transfer, and the unit has 2 workers × 8 threads = 16
  (`systemctl show movies -p ExecStart`).
- **`proxy_http_version 1.1`, which `location /` does not set.** `upstream gunicorn` already holds
  `keepalive 32` (line 10), but nginx talks HTTP/1.0 to upstreams unless told otherwise, and in 1.0
  that pool cannot be used and the `Connection ""` line means nothing: 1.0 asks for persistent
  connections with `Connection: keep-alive`, not by clearing the header. 1.1 plus the cleared header
  is the pair that switches the pool on, so the next segment reuses the previous segment's
  connection instead of opening a new one. With buffering off the `proxy_buffer_size` (line 116) and
  `proxy_buffers` (line 117) lines go too: they only size the buffering that was just removed
  (`proxy_buffer_size` still sizes the response-header buffer at its 4k default, ample for gunicorn).
- **300s read/send timeouts, where `location /` uses 120s (lines 113–114).** `proxy_read_timeout`
  counts the gap between reads *from gunicorn*, and with buffering off nobody reads from gunicorn
  while the client is slow — a slow client is exactly when that gap grows, so 120s would cut a stream
  that is merely waiting on the client. `proxy_connect_timeout 10s` is unchanged (line 112).

Not needed: `proxy_request_buffering` (the route is GET-only, there is no request body), and
`limit_conn connlimit 50;` at server level (line 80) already applies inside locations. `location /`
sets no `expires`, `access_log` or other directive this one would lose.

```nginx
# inside the server_name newmovies.linkpc.net block
location ^~ /proxy/ {
    limit_req zone=proxy_ratelimit burst=120 nodelay;
    include proxy_params;              # X-Forwarded-For feeds the per-IP limit (core/views.py:11931)
    proxy_pass http://gunicorn;
    proxy_http_version 1.1;            # nginx is 1.0 upstream by default; without this the pool (line 10) is dead
    proxy_set_header Connection "";    # only does anything together with the line above
    proxy_connect_timeout 10s;         # same as location / (line 112)
    proxy_read_timeout 300s;           # location / uses 120s (line 113); see the slow-client reason above
    proxy_send_timeout 300s;           # location / uses 120s (line 114)
    proxy_buffering off;               # location / uses on (line 115); no proxy_cache here to justify spooling
}
```

The `-e` referer in both commands is required once the app change is deployed: `/proxy/` is gated
by `core/proxy_guard.py`, which accepts a same-site `Referer`/`Origin` (or a signed `_pxt` token)
and answers `403` without one. With it the check means the same thing before and after.

Check: `curl -so /dev/null -w '%{http_code}\n' -r 0-10 -e https://newmovies.linkpc.net/
'https://newmovies.linkpc.net/proxy/example.com/'` → `200`, the value it gives today; anything
else means the location is wrong. Confirm the new lines are in the config nginx actually loaded, not
only in the file: `sudo nginx -T | grep -A11 'location \^~ /proxy/'` → the block above, containing
`proxy_http_version 1.1;` and `proxy_buffering off;` (`-T` prints the merged config, so it also
catches a second `/proxy/` location defined elsewhere).

Check the limiter by sending far more requests than the bucket holds. `burst=120` admits 120 at once
and then drains at 60 r/s, so a run of N requests that takes T seconds to send can answer at most
about `120 + 60×T` of them without a `429`; 600 requests over 40 parallel connections take roughly a
second, so expect ~180 answers that are not `429` and ~420 that are. Time the run, then check the
counts against that arithmetic:

```
time ( seq 1 600 | xargs -P40 -I{} curl -so /dev/null -w '%{http_code}\n' -e https://newmovies.linkpc.net/ 'https://newmovies.linkpc.net/proxy/example.com/' | sort | uniq -c )
```

Pass/fail: a run under ~5 s must show at least 250 `429`s. A non-`429` count far above
`120 + 60×T` means the limiter is not applied to that location — `sudo nginx -T | grep -A3
'location \^~ /proxy/'` prints the block nginx actually loaded; `503`s instead of `429`s mean
snippet 1 is missing, since `limit_req_status 429` is what turns the directive's default into
`429`. Django is not the source at this scale: its `/proxy/` budget is 3000/min per IP = 50 r/s
(`core/proxy_guard.py:56`), *below* the 60 r/s this bucket drains at, and its `429` body is a JSON
envelope while nginx's is its own error page. Unlike everything above, these counts are arithmetic
on nginx's documented leaky-bucket behaviour, not a measurement: applying the config is a change on
the box, which this document does not make.

Two things worth confirming on the box afterwards, both cheap:

- **Connection reuse.** gunicorn passes no `--keep-alive` (`systemctl show movies -p ExecStart` →
  `--workers 2 --threads 8 --worker-class gthread ...`), so it closes idle connections after its 2s
  default while nginx keeps them pooled for 60s. nginx drops a pooled socket it finds already
  closed rather than sending a request over it, so the effect of the mismatch is a shorter reuse
  window, not errors. If `sudo grep -c 'upstream prematurely closed' /var/log/nginx/error.log` is
  ever non-zero after this change, outlive nginx from the other side: add `--keep-alive 65` to
  `ExecStart` in the unit, then `sudo systemctl daemon-reload && sudo systemctl restart movies`. Not
  changed here — reuse already helps the bursty segment fetching either way.
- **The buffering decision, observed rather than inferred.** nginx's temp directory is root-only
  (`ls -ld /var/lib/nginx/proxy` → `drwx------ www-data`), so compare two throttled pulls with
  `sudo ls -l /var/lib/nginx/proxy` before and during each: a 24 MB `location /` download
  (`curl -s -o /dev/null --limit-rate 200k https://newmovies.linkpc.net/apps/download/3/`) is
  expected to spill a temp file once its 144 KB of buffers are full, and the same throttle on a real
  `/proxy/` segment URL must not. A small URL such as `proxy/example.com/` proves nothing here — it
  never fills a buffer. (Expected, not measured from here: reading that directory needs root.)

One separate issue surfaced while checking this block, deliberately not changed here:
`include proxy_params;` *appends* to `X-Forwarded-For` (`$proxy_add_x_forwarded_for`), and
`_get_client_ip` (`core/views.py:11931`) trusts the **first** value — so a client that sends its own
`X-Forwarded-For` picks its own rate-limit key, both on `/proxy/` and on the OTP limits that share
that helper (`core/views.py:11940`). Nothing in front rewrites the header (`curl -sI
https://newmovies.linkpc.net/` → `Server: nginx/1.18.0 (Ubuntu)`, no `cf-ray`/`via`, and no
`set_real_ip_from` in `/etc/nginx/`), so `$remote_addr` would be the truthful value there. That is
its own snippet, not this one.