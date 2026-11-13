# Renewing the `newmovies.linkpc.net` certificate

The certificate nginx served expired **2026-11-19**, nothing on the box tracked it
for renewal, and the cron line that was supposed to handle it pointed at a script
that was never installed. This document is the setup for that job: four files in
`deploy/ssl/` — three that are installed on the box, and one that installs them —
applied with the commands below. No server-side settings file has to be edited:
the expiry alarm (section 5) defaults to the chain nginx serves.

It is the "separate job" [`nginx-hardening.md`](nginx-hardening.md)
already refers to (section 2, which says so in as many words): snippet 2 opens
the ACME path under `/var/www/html`, and this document is what uses it.

Local-first, like the rest: the files live in this repo and are carried to the box
by `server\server_admin.bat 2`. Sections 1–6 are one script,
`deploy/ssl/apply-ssl-renewal.sh`, so the procedure is repeatable without retyping
it; **it was run on the box on 2026-11-13 and section 10 is that record** — the
renewed certificate, the installed cron entry and every check. What was and was not
verified locally is in section 9.

| File | Goes to | Purpose |
| --- | --- | --- |
| `deploy/ssl/ssl_deploy_cert.sh` | `/usr/local/bin/ssl_deploy_cert.sh` | certbot deploy hook: copies the new chain to `/etc/ssl/custom`, `nginx -t`, reload |
| `deploy/ssl/ssl_auto_renew.sh` | `/usr/local/bin/ssl_auto_renew.sh` | `certbot renew` + a log line saying what is actually deployed |
| `deploy/ssl/ssl-renewal.cron` | `/etc/cron.d/ssl-renewal` | the monthly backstop that replaces the broken user-crontab line |
| `deploy/ssl/apply-ssl-renewal.sh` | — (run in place) | sections 1–6 unattended and idempotent, with the checks built in |
| `movie_portal/settings.py` (already in the repo) | — | `HEALTH_CERT_PATHS` defaults to the chain nginx serves, so the expiry alarm needs no server edit |

---

## 0. What is wrong today

All five observed on the box on 2026-11-13.

**The certificate is five days from expiry and is not the one Let's Encrypt would
renew.**

```console
$ openssl x509 -in /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer -noout -subject -issuer -enddate
subject=CN = newmovies.linkpc.net
issuer=C = US, O = Let's Encrypt, CN = YR1
notAfter=Nov 19 02:11:38 2026 GMT
```

nginx reads exactly that file:

```console
$ grep -n "ssl_certificate" /etc/nginx/sites-enabled/movies
73:    ssl_certificate     /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer;
74:    ssl_certificate_key /etc/ssl/custom/newmovies.linkpc.net.key;
```

**Nothing in `/etc/letsencrypt` can renew it.**

```console
$ ls /etc/letsencrypt/renewal/ ; echo "exit $?"
ls: cannot access '/etc/letsencrypt/renewal/': No such file or directory
exit 2
$ ls -A /etc/letsencrypt/
cli.ini  renewal-hooks
$ ls -A /etc/letsencrypt/renewal-hooks/deploy/ ; echo "exit $?"
exit 0
```

There is no lineage directory, no `renewal/` and **no `accounts/`** — certbot has
never completed a registration on this box. The files in `/etc/ssl/custom/` were
placed by hand on Aug 21 and are static:

```console
$ ls -l /etc/ssl/custom/
-rw-r--r-- 1 ubuntu ubuntu 1452 Aug 21 04:18 newmovies.linkpc.net.cer
-rw-r--r-- 1 ubuntu ubuntu  944 Aug 21 04:18 newmovies.linkpc.net_ca.cer
-rw-r--r-- 1 root   root   6000 Aug 21 04:18 newmovies.linkpc.net_fullchain.cer
-rw------- 1 ubuntu ubuntu 1828 Aug 21 04:18 newmovies.linkpc.net.key
```

The last two are the pair nginx loads. `.cer` and `_ca.cer` are referenced
nowhere (`grep -rn "newmovies.linkpc.net.cer\|_ca.cer" /etc/nginx` → no match)
and are left alone. `/home/ubuntu/ssl/ip_access.cer`, which the other server
block loads, is valid to 2036 and is not touched either.

**The cron line cannot work where it is.** It sits in *ubuntu's* crontab:

```console
$ crontab -l | grep -n ssl
1:0 3 1 * * /usr/local/bin/ssl_auto_renew.sh >> /var/log/ssl_renewal.log 2>&1
```

The script does not exist, and every step of what it would do needs root: certbot
writes `/etc/letsencrypt`, the ACME challenge goes under root-owned
`/var/www/html`, the deploy hook writes `/etc/ssl/custom` and reloads nginx. Run
as ubuntu it would fail on all four.

**`/etc/cron.d/certbot` is inert.** it is the 2018 packaging leftover whose own
command is commented out in favour of the systemd timer:

```console
$ ls -l /etc/cron.d/ ; systemctl list-timers certbot.timer --all | head -3
-rw-r--r-- 1 root root 1024 Aug  3  2018 certbot
-rw-r--r-- 1 root root  201 Feb 16  2020 e2scrub_all
NEXT                        LEFT        LAST                        PASSED  UNIT
Fri 2026-11-13 21:55:00 UTC 9h          Fri 2026-11-13 07:12:00 UTC 5h ago  certbot.timer
```

`certbot.timer` is the primary renewal path and stays the primary path — it runs
`certbot -q renew` **twice a day** with a random delay:

```console
$ systemctl cat certbot.timer | grep -E "OnCalendar|RandomizedDelay"
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=43200
Persistent=true
```

**The ACME path is closed.** Until snippet 2 of `nginx-hardening.md` is applied,
the challenge URL never reaches a file: the port-80 `newmovies.linkpc.net` block
proxies *everything* to gunicorn, so Django's `SECURE_SSL_REDIRECT` answers it with
a **301** to https:

```console
$ curl -s -o /dev/null -w '%{http_code}\n' http://newmovies.linkpc.net/.well-known/acme-challenge/test
301
```

A 301 is as fatal to `certbot --webroot` as a 403 would be — the key authorisation
is not in the body. Snippet 2 also removes the bare `listen 80;` from that block,
because the exception needs port 80 to have its own block for this name.

---

## 1. Prerequisite: snippet 2, and how to prove it

Section 2 of [`nginx-hardening.md`](nginx-hardening.md) adds the
`location ^~ /.well-known/acme-challenge/` block that serves
`/var/www/html/.well-known/acme-challenge/` as `text/plain`. Apply it first, then
prove the mapping end to end — a 403 here means `certbot --webroot` will fail and
nothing after this point is worth running:

```console
$ sudo mkdir -p /var/www/html/.well-known/acme-challenge
$ echo precheck-ok | sudo tee /var/www/html/.well-known/acme-challenge/precheck
$ curl -s http://newmovies.linkpc.net/.well-known/acme-challenge/precheck
precheck-ok
$ sudo rm /var/www/html/.well-known/acme-challenge/precheck
$ curl -s -o /dev/null -w '%{http_code}\n' http://newmovies.linkpc.net/.well-known/acme-challenge/precheck
404
```

Expected: the body of the file comes back (not a 403, not the app's HTML), and
after removing it a 404 — the 404 proves the file was being served from disk
rather than by a catch-all. This URL is exactly what certbot's HTTP-01 validation
fetches; it is also in `nginx-hardening.md`'s own check.

---

## 2. Install the three files

After the usual pull (`server\server_admin.bat 2`), on the box:

```console
$ cd /home/ubuntu/project/movies
$ sudo install -m 0755 deploy/ssl/ssl_deploy_cert.sh /usr/local/bin/ssl_deploy_cert.sh
$ sudo install -m 0755 deploy/ssl/ssl_auto_renew.sh  /usr/local/bin/ssl_auto_renew.sh
$ sudo install -m 0644 deploy/ssl/ssl-renewal.cron   /etc/cron.d/ssl-renewal
$ ls -l /usr/local/bin/ssl_deploy_cert.sh /usr/local/bin/ssl_auto_renew.sh /etc/cron.d/ssl-renewal
-rwxr-xr-x 1 root root 4380 Nov 13 12:00 /usr/local/bin/ssl_deploy_cert.sh
-rwxr-xr-x 1 root root 2401 Nov 13 12:00 /usr/local/bin/ssl_auto_renew.sh
-rw-r--r-- 1 root root  830 Nov 13 12:00 /etc/cron.d/ssl-renewal
```

(The byte counts are the repo files'; the timestamp is whenever you ran
`install`. Every line must be `root root`.)

`chmod 0755` matters: certbot only runs hooks it can execute. `0644 root:root` on
the cron file matters too — cron ignores a `/etc/cron.d` file that is writable by
anyone else.

**The deploy hook is *not* copied into `/etc/letsencrypt/renewal-hooks/deploy/`,
deliberately.** From this box's certbot (1.21.0):

* a first `certonly` runs only the `--deploy-hook` given on its command line and
  never reads that directory — `_internal/main.py:137`,
  `hooks.deploy_hook(config, ...)` inside the fresh-issuance branch;
* a later `certbot renew` runs the directory's hooks **and** the lineage's
  `renew_hook`, skipping the second only if the strings are *identical*
  (`_internal/hooks.py:190`, `_run_deploy_hook`), so a second copy under a
  different path would run the deploy twice per renewal.

One copy, named once by `--deploy-hook`, is therefore the whole mechanism, and
certbot writes that flag into the lineage as `renew_hook` (see section 4).

```console
$ ls -A /etc/letsencrypt/renewal-hooks/deploy/
$ # still empty — that is the intent, not an omission
```

---

## 3. Issue the lineage

Run the dry run first. It exercises the account, the ACME path and the challenge
without touching `/etc/ssl/custom`, because certbot skips deploy hooks under
`--dry-run` (`hooks.py:213`, *"Dry run: skipping deploy hook command"*):

```console
$ sudo certbot certonly --dry-run \
    --webroot -w /var/www/html \
    -d newmovies.linkpc.net --cert-name newmovies.linkpc.net \
    --deploy-hook /usr/local/bin/ssl_deploy_cert.sh \
    --non-interactive --agree-tos --register-unsafely-without-email
Successfully received certificate.
...
The dry run was successful.
```

Then the same command without `--dry-run`:

```console
$ sudo certbot certonly \
    --webroot -w /var/www/html \
    -d newmovies.linkpc.net --cert-name newmovies.linkpc.net \
    --deploy-hook /usr/local/bin/ssl_deploy_cert.sh \
    --non-interactive --agree-tos --register-unsafely-without-email
Successfully received certificate.
Certificate is saved at:         /etc/letsencrypt/live/newmovies.linkpc.net/fullchain.pem
Key is saved at:                 /etc/letsencrypt/live/newmovies.linkpc.net/privkey.pem
This certificate expires on 2027-02-11.
These files will be updated when the certificate renews.
```

What each part is for:

* **`--webroot -w /var/www/html`** — HTTP-01 through the directory snippet 2
  serves. `/var/www/html` is not the app's static root; the app is untouched by
  this and no request ever reaches Django.
* **`--cert-name newmovies.linkpc.net`** — pins the lineage name so the live path
  is `/etc/letsencrypt/live/newmovies.linkpc.net/`, and so the hook's default
  target names line up: `…/fullchain.pem` → `/etc/ssl/custom/newmovies.linkpc.net_fullchain.cer`
  and `…/privkey.pem` → `/etc/ssl/custom/newmovies.linkpc.net.key`.
* **`-d newmovies.linkpc.net` only** — the current certificate is *twice* as
  broad and that cannot be kept:

  ```console
  $ openssl x509 -in /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer -noout -ext subjectAltName
  X509v3 Subject Alternative Name:
      DNS:*.newmovies.linkpc.net, DNS:newmovies.linkpc.net
  ```

  A wildcard SAN can only be authorised with `dns-01`; this box has no DNS API
  credentials, so the new lineage covers the apex name alone. Nothing is lost:
  no subdomain other than `www` resolves at all, and `www` is already broken on
  its own — port 80 answers `400` and SNI falls through to the default block's
  self-signed `ip_access.cer`, and `ALLOWED_HOSTS` in the server's
  `movie_portal/settings_local.py` does not contain `www`. A wildcard that
  matched nothing but a broken hostname is not worth a DNS plugin.
* **`--deploy-hook /usr/local/bin/ssl_deploy_cert.sh`** — runs once the lineage
  exists, with `RENEWED_LINEAGE` set to the live directory (`hooks.py:216`). That
  is the run that puts the certificate into `/etc/ssl/custom` and reloads nginx.
* **`--non-interactive --agree-tos`** — no TTY on a remote shell.
* **`--register-unsafely-without-email`** — there is no ACME account on this box
  and no address in this repo to use. This creates the account without one, at
  the cost of Let's Encrypt's own expiry/revocation mail. Its substitute is the
  alarm in section 5, which fires at **21 days** — before the first notice
  Let's Encrypt itself would send. If you would rather have the mail, use
  `--email you@example.com` in place of the last flag; this build offers no
  separate way to attach an address to an account later
  (`certbot --help all | grep -c update_account` → `0`).

Do not add `--no-random-sleep-on-renew` to anything: this certbot has no such
option (`certbot --help all | grep -c no-random-sleep-on-renew` → `0`) and an
unknown flag aborts the whole run. The `ssl_auto_renew.sh` header says the same.

---

## 4. Verify the install

The lineage exists, is webroot-based, and remembers the hook:

```console
$ sudo certbot certificates
Certificate Name: newmovies.linkpc.net
    Domains: newmovies.linkpc.net
    Expiry Date: 2027-02-11 (VALID: 89 days)
    Certificate Path: /etc/letsencrypt/live/newmovies.linkpc.net/fullchain.pem
    Private Key Path: /etc/letsencrypt/live/newmovies.linkpc.net/privkey.pem

$ sudo grep -E "^(authenticator|webroot_path|renew_hook|key_type)" /etc/letsencrypt/renewal/newmovies.linkpc.net.conf
authenticator = webroot
webroot_path = /var/www/html,
renew_hook = /usr/local/bin/ssl_deploy_cert.sh
```

There is no `key_type` line: this build (1.21.0) writes the default, and
`certbot certificates` reports `Key Type: RSA` for the lineage issued on
2026-11-13. That is fine — nginx serves it as it served the old pair, and the
run in section 10 confirmed the handshake.

`renew_hook` is the important line: it is the `--deploy-hook` flag persisted
under its config-file name (`cli_constants.py:81` maps `renew_hook` ↔
`deploy_hook`), and it is what makes the twice-daily, headless `certbot renew`
copy the chain into place. **If it is missing, renewal succeeds and the site
quietly keeps serving the old certificate** — re-run the `certonly` command with
`--deploy-hook` (or add the line by hand) and check again.

The hook ran and installed the pair:

```console
$ sudo ls -l /etc/ssl/custom/ | awk '{print $1, $3, $4, $9}'
-rw-r--r-- ubuntu ubuntu newmovies.linkpc.net.cer
-rw-r--r-- ubuntu ubuntu newmovies.linkpc.net_ca.cer
-rw-r--r-- root   root   newmovies.linkpc.net_fullchain.cer
-rw------- root   root   newmovies.linkpc.net.key
drwx------ root   root   previous
$ sudo ls -l /etc/ssl/custom/previous/ | awk '{print $1, $3, $4, $9}'
-rw-r--r-- root root fullchain.cer.old
-rw------- root root newmovies.linkpc.net.key.old
```

`awk` drops the two columns that change on every issuance (size, mtime) — what
matters here is the mode and owner. The `.cer` and `_ca.cer` lines are the old
leaf and intermediate this change does not touch, so they keep their original
`ubuntu` owner; only the pair nginx loads becomes `root root`.

The chain is `0644` on purpose and the key `0600`; both are now `root:root`,
where the key it replaces was `ubuntu:ubuntu 0600`. That is a deliberate
tightening, not an accident: nginx reads both at configuration load and nginx's
master is root (`ps -o user=,cmd= -C nginx` → `root … nginx: master process`),
while the workers are `www-data` and never open them. The `0644` on the chain is
also what the app user needs for the alarm in section 5. `previous/` holds the
pair that was live before this deploy, kept for a rollback (section 8).

nginx is now serving the new chain — compare what is on disk with what is
actually being presented. Note `-servername`: without it the TLS handshake lands
on the default server block and you would be comparing against `ip_access.cer`:

```console
$ sudo openssl x509 -in /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer -noout -fingerprint -sha256
SHA256 Fingerprint=AA:BB:…:FF
$ echo | openssl s_client -connect 127.0.0.1:443 -servername newmovies.linkpc.net 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha256
SHA256 Fingerprint=AA:BB:…:FF
$ curl -sI https://newmovies.linkpc.net/ | head -1
HTTP/1.1 403 Forbidden
```

Equal fingerprints mean the reload happened *and* the named block picked the file
up. The `403` is the app's own answer (it rejects non-browser clients) — a
completed handshake, which is what matters here; a certificate problem would
fail before any status line.

Finally, prove the renewal path without waiting three months:

```console
$ sudo certbot renew --dry-run
Congratulations, all simulated renewals succeeded.
$ sudo grep -c "skipping deploy hook" /var/log/letsencrypt/letsencrypt.log
1
```

"skipping deploy hook" is the expected line here (certbot 1.21 refuses to run
deploy hooks in a dry run), and it is why the dry run cannot disturb
`/etc/ssl/custom/`: re-run the fingerprint command above and it is unchanged.
What the dry run does prove is the part that matters most over time — account,
webroot, challenge and the renewal config all work unattended.

---

## 5. The expiry alarm

certbot's own failure mode is silent: a hook that refuses is *reported* and the
run still exits `0` (`compat/misc.py:163`, `subprocess.run(..., check=False)`),
and the test certbot installs in a dry run is refused by the hook. So the alarm
watches the file nginx actually serves, not certbot's copy: any renewal that
succeeds on certbot's side without reaching `/etc/ssl/custom` shows up as a
countdown on the deployed file.

Nothing to configure on the box: `movie_portal/settings.py` now defaults
`HEALTH_CERT_PATHS` to exactly the file nginx serves (config line 73, quoted above), so the alarm
is on for every deployment — including one where nobody remembered to edit the
gitignored `settings_local.py`, which is how it was skipped before. That file
still overrides the setting (it is imported last), but that is the escape hatch,
not a step:

```console
$ cd /home/ubuntu/project/movies
$ grep -n HEALTH_CERT_PATHS movie_portal/settings.py     # the default, in the repo
246:HEALTH_CERT_PATHS = ['/etc/ssl/custom/newmovies.linkpc.net_fullchain.cer']
$ ./venv/bin/python manage.py check_health --no-send ; echo "exit $?"
[PROBLEM] TLS certificate — /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer expires in 5 days (Nov 19 02:11:38 2026 GMT, warn at 21 days)
...
exit 1
```

That output is today; after section 3 the same line reads `[OK] … expires in 89
days` and the exit status is `0`. How it works:

* The threshold is `CERT_WARN_DAYS = 21` in `core/health.py` (`~line 84`). It is
  below certbot's 30-day renewal window, so a healthy box is at 20–30 days when
  the alarm fires — 21 days means a renewal run has failed at least twice.
* The path list lives in `HEALTH_CERT_PATHS` (`movie_portal/settings.py:246`).
  The default is the deployed chain, so no deployment has to remember anything;
  `settings_local.py` overrides it, and `[]` switches the check off (an empty
  list is not a false alarm). Several files may be listed — the
  soonest-expiring readable one is the one reported.
* A file that cannot be read at all reports `[unknown]` rather than a failure —
  and is therefore *not* alerted on. The chain is installed `0644` so the app
  user can read it; if that mode is ever lost, the check goes quiet instead of
  failing. `ls -l` is covered above for exactly that reason.
* Delivery is the existing engine, no new model, migration, template or view: the
  finding's key is `cert`, incidents are recorded through
  `core/health.py:record_incidents`, and an `AlertRecipient` whose `checks` field
  is empty hears every check (`core/models.py`, `AlertRecipient.wants()`). The
  live recipient `alokkumar2812@gmail.com` has `checks = ''`, so it receives the
  alert already; a recipient that lists keys explicitly must include `cert`.
* `check_health` already runs every 5 minutes from ubuntu's crontab, and
  `repeat_minutes = 30` suppresses duplicate emails, so a failing certificate
  mails at most twice an hour and a recovery closes the incident without a mail.

A test mail on demand (this ignores the pass/fail of the checks, so it will send
even while everything is fine):

```console
$ ./venv/bin/python manage.py check_health --force
```

---

## 6. Cron: replace the line that could never work

The root-owned entry in `deploy/ssl/ssl-renewal.cron` is the backstop;
`certbot.timer` remains the primary path. Install it as shown in section 2, then
remove the broken user-crontab line — the root entry replaces it at the same
monthly cadence, so this is not a feature being silenced:

```console
$ crontab -l | grep -c ssl_auto_renew          # 1 — the broken line
$ crontab -l | grep -v ssl_auto_renew | crontab -
$ crontab -l | grep -c ssl_auto_renew          # 0
```

```console
$ sudo ls -l /etc/cron.d/ssl-renewal
-rw-r--r-- 1 root root 684 Nov 13 12:00 /etc/cron.d/ssl-renewal
$ sudo cat /etc/cron.d/ssl-renewal | tail -1
17 3 1 * * root /usr/local/bin/ssl_auto_renew.sh >> /var/log/ssl_renewal.log 2>&1
$ sudo tail -c 1 /etc/cron.d/ssl-renewal | od -c | head -1
0000000  \n
```

Three things to see: `root` in the user field (the whole point), mode `0644`
root-owned, and a trailing newline — cron silently ignores a `/etc/cron.d` file
without one. `SHELL=/bin/bash` and an explicit `PATH` are set in the file because
`/etc/cron.d` entries do not inherit a login environment.

Run it once by hand; outside the 30-day renewal window it does nothing to the
certificate and exits quickly, which also confirms the logging:

```console
$ sudo /usr/local/bin/ssl_auto_renew.sh ; echo "exit $?"
2026-11-14T03:17:02Z ssl-renew: renewing: certbot 1.21.0
2026-11-14T03:17:02Z ssl-renew: deployed certificate notAfter Nov 19 02:11:38 2026 GMT (/etc/ssl/custom/newmovies.linkpc.net_fullchain.cer)
exit 0
$ tail -3 /var/log/ssl_renewal.log
$ journalctl -t ssl-renew -n 3
```

The `notAfter` line is the point of the script: it reports what is *deployed*,
so a stale date in the log is visible evidence that a renewal never reached
`/etc/ssl/custom` — the one failure the exit status will not tell you about. The
script refuses to run as non-root (`id -u` guard) and propagates certbot's exit
status, so a real failure is non-zero in both cron's mail and the log.

---

## 7. What a renewal looks like from here

Nothing further is needed on the box. The twice-daily timer runs `certbot -q
renew`; inside the 30-day window (from roughly 2027-01-12 for a certificate
expiring 2027-02-11) certbot issues a new chain, then runs the lineage's
`renew_hook`:

1. the hook copies the new `fullchain.pem` and `privkey.pem` to
   `/etc/ssl/custom/*.new`, `mv`s them over the live pair, having first copied the
   working pair into `previous/`;
2. `nginx -t` must accept them;
3. `systemctl reload nginx` swaps them in with no dropped connection.

There is no application restart and none is needed — nothing in the Django app
reads these files (`grep -rn "ssl/custom" --include='*.py' .` matches only the
comment in `core/health.py`), so gunicorn is untouched.

If step 1 or 2 fails, the hook puts the old pair back and exits non-zero; certbot
reports the failed hook but **still exits 0** (`compat/misc.py:163`), which is
exactly why section 5's alarm exists. Hook output lands in
`/var/log/letsencrypt/letsencrypt.log`; the cron path additionally writes
`/var/log/ssl_renewal.log`.

---

## 8. Rollback

Put the previous pair back (copy, not move, so the backups survive) and reload:

```console
$ sudo cp -p /etc/ssl/custom/previous/fullchain.cer.old /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer
$ sudo cp -p /etc/ssl/custom/previous/newmovies.linkpc.net.key.old /etc/ssl/custom/newmovies.linkpc.net.key
$ sudo nginx -t && sudo systemctl reload nginx
$ sudo openssl x509 -in /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer -noout -enddate
notAfter=Nov 19 02:11:38 2026 GMT
```

To stop renewing entirely, remove the pieces in reverse order:

```console
$ sudo certbot delete --cert-name newmovies.linkpc.net
$ sudo rm /etc/cron.d/ssl-renewal
$ sudo rm /usr/local/bin/ssl_deploy_cert.sh /usr/local/bin/ssl_auto_renew.sh
$ # then switch the alarm off: HEALTH_CERT_PATHS = [] in movie_portal/settings_local.py
```

`certbot delete` removes only the lineage under `/etc/letsencrypt/live/` and its
`renewal/` config; the deployed files in `/etc/ssl/custom/` are what nginx serves,
so the site keeps working from them until the certificate itself expires. The
backup pair in `previous/` is written on every deploy, and a *rejected* deploy
consumes the pair it wrote by moving it back — after a rollback it is gone until
the next successful install.

---

## 9. What was and was not verified, and the deliberate choices

Run on the box, read-only: certificate dates, SANs and issuer; the nginx
`ssl_certificate` lines; the `/etc/ssl/custom/` listing; the absence of
`/etc/letsencrypt/{renewal,accounts}`; the crontab line; `/etc/cron.d/`;
`certbot.timer`; the challenge URL (`301`, not the 403 an earlier draft of this
document assumed); `certbot --version`; and the certbot
1.21.0 source that fixes the hook behaviour — `_internal/main.py:137` (fresh
issuance runs only `--deploy-hook`), `_internal/hooks.py:190` (path-based
de-duplication) and `:213` (dry runs skip deploy hooks), `compat/misc.py:163`
(a failing hook does not fail the run), and `cli_constants.py:81`
(`deploy_hook` ↔ `renew_hook`). `ps` for the nginx master user.

Verified locally, on this machine: both scripts parse (`bash -n`); the deploy
hook's branches against a real certificate made with `openssl req -x509`, stubbed
`nginx -t` and a stubbed reload command (`RELOAD_CMD`) — 34 assertions over 11
cases, all passing: first install writes both files, backs up the old pair and
reloads exactly once; a re-run with identical files copies nothing and does not
reload; a staging-issuer chain is refused before anything is touched; a failing
`nginx -t` restores the previous pair, leaves no `.new` files and exits 1; a
missing lineage is a no-op with exit 0; an unreadable issuer exits 1; a failing
reload exits 1 with the new files in place. The renewal script under stub
binaries — a non-root uid is refused before certbot is called, root with a
readable certificate logs the `notAfter` line and exits 0, certbot's non-zero
status is propagated, and a missing `CERT_FILE` exits 1. The
app side: `manage.py check`, `makemigrations --check` ("No changes detected"),
and `check_health --no-send` three ways — with the new default on this machine,
which has no such file (the laptop case), it prints `[unknown] TLS certificate —
no readable certificate at /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer`
and still exits 0, so a machine that serves no certificate is unaffected; with
`HEALTH_CERT_PATHS = []` there is no `cert` row at all; pointed at a real chain it
gives the `[PROBLEM] ... expires in 5 days` finding and exit 1, a 90-day chain
`[OK] ... expires in 89 days`, and an unreadable path `unknown`.

Not verified when this was written, because it needs the box: no `certbot` or
`nginx` runs (nothing was installed, issued or reloaded), so the sections above
were instructions to run, not a record of a run. **They were then run on the box
on 2026-11-13: section 10 is that record, and it is where the box-side facts
(file modes, lineage config, the deployed date, the cron entry) now live.** File
modes from `install -m 0600/0700` could not be checked on NTFS, so they were
confirmed on the box instead. The 1.21.0 source lines above are read from the
installed package; the issued-lineage config (`webroot_path`, `renew_hook`) is
written by certbot itself and was checked with section 4's `grep` on the box.

Deliberate choices, restated: the wildcard SAN is dropped (it needs `dns-01`);
`www` is not added back (already broken, and absent from `ALLOWED_HOSTS`); the
unused `.cer`/`_ca.cer` and the 2036 `ip_access.cer` are left alone; the key's
ownership moves from `ubuntu:ubuntu` to `root:root` (`0600`, read by nginx's root
master); the new lineage is created with `--cert-name newmovies.linkpc.net` so it
lands on the same live paths the hook and the alarm expect; and the renewal
script does not pass `--no-random-sleep-on-renew`, which this certbot does not
have. The ACME account is created without an email address, so Let's Encrypt
sends no expiry notices — the 21-day alarm is this setup's substitute, and it
fires before Let's Encrypt's own first notice would.

---

## 10. Applied on the box, 2026-11-13

The whole of sections 1–6 was run as ubuntu on `151.145.57.106` through
`deploy/ssl/apply-ssl-renewal.sh` (every privileged step is a `sudo -n` inside
that file, and it stops at the first step if passwordless sudo is not available).
It is idempotent, so re-running it re-installs the same files and re-checks the
same things.

### Why neither renewal path had ever done anything

* **`certbot.timer` had nothing to renew.** `/etc/letsencrypt` held only
  `cli.ini` and an empty `renewal-hooks/deploy/` — no `live/`, no `renewal/`, no
  `accounts/`. Its twice-daily `certbot -q renew` exited `0` in about 1.3 s
  having attempted nothing (`journalctl -u certbot`: *Starting Certbot ...
  Deactivated successfully*), so the timer was healthy and the lineage it needs
  did not exist. Section 3 creates it.
* **The user-crontab line did fire, and failed.** syslog caught its only run:

  ```console
  $ zgrep -h ssl_auto_renew /var/log/syslog*
  Nov  1 03:00:01 instance-20260702-2010 CRON[284565]: (ubuntu) CMD (/usr/local/bin/ssl_auto_renew.sh >> /var/log/ssl_renewal.log 2>&1)
  ```

  Four reasons it could not have worked: nothing matching `ssl` was ever in
  `/usr/local/bin`; the entry runs as ubuntu, where the script's `id -u` guard
  refuses; the redirect could not create `/var/log/ssl_renewal.log` because
  `/var/log` is root-owned; and a monthly cadence fired 2026-11-01 and would next
  fire 2026-12-01, after the 2026-11-19 expiry. The repo's
  `/etc/cron.d/ssl-renewal` was never installed (`ls /etc/cron.d/` →
  `.placeholder`, `certbot`, `e2scrub_all`).

### What was run, and what came back

| Step | Result |
| --- | --- |
| snippet 2 (`nginx-hardening.md`) | applied to `/etc/nginx/sites-enabled/movies`: ACME block appended, bare `listen 80;` removed, `nginx -t` ok, reloaded; backup in `/tmp/movies.nginx.*.bak` |
| ACME path proof | `http://newmovies.linkpc.net/.well-known/acme-challenge/precheck` → `precheck-ok`, then `404` after removal |
| three files installed | `/usr/local/bin/ssl_deploy_cert.sh` `0755 root root`; `/usr/local/bin/ssl_auto_renew.sh` `0755 root root`; `/etc/cron.d/ssl-renewal` `830 B 0644 root root` |
| `certonly --dry-run` | *The dry run was successful.* (no deploy hook ran) |
| `certonly` | *Successfully received certificate ... expires on 2027-02-11* |
| deploy hook | `ssl-deploy: installed /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer (notAfter Feb 11 12:04:54 2027 GMT) and reloaded nginx` |
| lineage | `Certificate Name: newmovies.linkpc.net`, `Key Type: RSA`, `Expiry Date: 2027-02-11 12:04:54+00:00 (VALID: 89 days)`, plus `authenticator = webroot`, `webroot_path = /var/www/html,`, `renew_hook = /usr/local/bin/ssl_deploy_cert.sh` |
| deployed pair | `-rw-r--r-- root root ..._fullchain.cer`, `-rw------- root root ....key`, `drwx------ root root previous/` (the Aug 21 `.cer`/`_ca.cer` are untouched) |
| fingerprints | on disk and served are both `EC:8A:81:12:23:86:4F:D0:10:2A:18:58:0E:ED:0D:0D:0F:74:30:57:71:29:84:D7:08:B6:6E:6A:61:86:F3:13` |
| expiry now | `notAfter=Feb 11 12:04:54 2027 GMT` on disk, over the public handshake, and in the alarm |
| alarm | `[OK] TLS certificate — /etc/ssl/custom/newmovies.linkpc.net_fullchain.cer expires in 89 days (Feb 11 12:04:54 2027 GMT, warn at 21 days)`, `check_health --no-send` exit `0` |
| cron in | `/etc/cron.d/ssl-renewal` last line `17 3 1 * * root /usr/local/bin/ssl_auto_renew.sh >> /var/log/ssl_renewal.log 2>&1`, trailing `\n` present |
| cron out | ubuntu crontab `ssl` lines: `1` → `0` |
| hand run | `ssl_auto_renew.sh` exit `0`; `journalctl -t ssl-renew`: `renewing: certbot 1.21.0`, `deployed certificate notAfter Feb 11 12:04:54 2027 GMT` |
| `renew --dry-run` | *Congratulations, all simulated renewals succeeded: /etc/letsencrypt/live/newmovies.linkpc.net/fullchain.pem (success)*, `no renewal failures`, `Dry run: skipping deploy hook command: /usr/local/bin/ssl_deploy_cert.sh` — and the deployed fingerprint was unchanged afterwards |
| services | `nginx` active, `movies` active, `certbot.timer` active |
| from off the box | `http://…/` → `301`; `http://…/.well-known/acme-challenge/x` → `404` with no `Location:` header; `https://…/` → `200`; `openssl s_client` → `notAfter=Feb 11 12:04:54 2027 GMT`, issuer `CN=YR2` |

### Five things the run taught

1. **`sed -i` cannot edit `sites-enabled/movies`.** The file is ubuntu-owned but
   its directory is root-owned, and `sed -i` writes its temporary file next to the
   target. The installer builds the new config in `/tmp` and copies it over the
   file. Backups go to `/tmp` too: a `*.bak` file left in `sites-enabled/` is
   loaded by the `sites-enabled/*` include and breaks `nginx -t`.
2. **The precheck is racy straight after a reload.** Old nginx workers keep
   accepting connections for a moment and answered the first probe out of the
   previous config (`301`). The installer retries the probe up to ten times.
   Do not read one probe as the verdict.
3. **The ACME location answers `Content-Type: application/octet-stream`**, not
   `text/plain`: nginx takes the type from the extension and `.precheck` is not in
   `mime.types`. certbot fetches the body and does not care, and its own tokens
   have no extension either.
4. **`/var/log/ssl_renewal.log` will not exist until the cron entry runs**
   (next: 2026-12-01 03:17) — the redirect is cron's. The hand run's evidence is
   the journal, `journalctl -t ssl-renew`, as quoted above.
5. **`certbot renew` delays itself before doing anything.** The dry run above
   took about eight minutes from invocation to `2027-02-11`-style success
   (`13:05` → `13:12:42`), so it must not be wrapped in a short timeout, and an
   ssh session that seems to hang is not a hang.

The next scheduled events are `certbot.timer` (twice daily; it now finds a lineage
and renews inside the 30-day window from roughly 2027-01-12) and
`/etc/cron.d/ssl-renewal` on the 1st of each month at 03:17 as the backstop.

One scope change to keep in mind: this lineage is obtained over HTTP-01 (webroot),
so it covers `newmovies.linkpc.net` alone, where the hand-placed August file also
carried `*.newmovies.linkpc.net`. A subdomain that was relying on that wildcard
would need a DNS-01 lineage of its own — the wildcard cannot be renewed through
the webroot challenge.
