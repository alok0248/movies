# Android API client validation

This is the reference for the device and app checks the server runs on the
Android app's API traffic: how the chain works, what each refusal looks like on
the wire, and how to keep it switched on safely over time.

Code: `core/client_validation.py` (the checks), `core/models.py`
(`AndroidApp` fields and `AppClientValidationLog`), `core/views.py`
(`_validate_android_auth`, `android_app_endpoint`, `android_app_settings`).
Tests: `core/tests/test_client_validation.py`.

---

## 1. What this can and cannot prove — read this first

Only two things in the chain are proof:

* **the app's Basic credential** (`access_username` / `access_password` on the
  `AndroidApp` row), which the server can check because it stores it; and
* **the per-user JWT** (`X-User-Token`), which the server signs.

Everything else — platform, package name, installer, signing-certificate
fingerprint, tamper/emulator/root flags — is a **header the client writes**. A
client that wants to lie can set any of them. So the checks are a *bar-raiser*,
not cryptographic proof of the app's identity:

* they stop the cheap attacks — a script replaying the API, a browser replaying
  a captured request, a clone built from a decompiled APK, an emulator farm —
  because each has to first learn and then reproduce values that only the real
  app sends;
* they do **not** stop an attacker who can read the APK, because a value the
  client can read is a value the client can repeat.

**The only real attestation is Google Play Integrity API**, where Google signs a
verdict containing the package name, the certificate digest and an
"app-recognised" flag that the client cannot forge. The hook for it is
`core/client_validation.py::expose_integrity_verdict`, deliberately left as a
no-op rather than faked: implement it when the app is updated to request an
integrity token, verify that token with Google, and compare the package and
certificate digest inside the verdict against the same allow-lists used here.
Until then, treat this layer as one of several, not as a lock on its own.

HMAC request signing (`require_request_signature`) raises the bar further —
a captured request cannot be replayed and a proxied body cannot be altered —
but the secret lives in the APK, so it is a speed bump for a determined
attacker, not a wall.

---

## 2. The chain

Every Android-facing endpoint calls `_validate_android_auth(request)`, which
now ends with `enforce_app_client(request, app)`. (The older
`/api/android-apps/<slug>/` endpoint runs the same call after its own credential
check.) Checks run in this order and the first failure ends the request:

| # | Check | Refusal `code` |
|---|-------|----------------|
| 1 | `X-Android-App` matches the credential's app slug | `app_identity_mismatch` |
| 2 | `X-Android-Platform: android` present and correct | `missing_platform_header`, `platform_not_android` |
| 3 | No browser-only headers (`Origin`, `Sec-Fetch-*`) | `browser_client` |
| 4 | `X-Android-Package` in `allowed_package_names` | `missing_package_header`, `package_mismatch` |
| 5 | `X-Android-Installer` in `allowed_installers` | `missing_installer_header`, `installer_not_allowed` |
| 6 | `X-Android-Signature-Sha256` in `allowed_signing_cert_sha256` | `missing_signature_header`, `signature_mismatch` |
| 7 | Client did not report tampering (`X-Android-Tampered`) | `tamper_detected` |
| 8 | Not a debuggable build (`X-Android-Debug`) | `debug_build` |
| 9 | Not an emulator (`X-Android-Emulator`) | `emulator_detected` |
| 10 | Not a rooted device (`X-Android-Rooted`) | `rooted_device` |
| 11 | HMAC signature valid, fresh, not replayed (optional) | `missing_request_signature`, `stale_request`, `replayed_request`, `invalid_request_signature`, `signature_not_configured` |

A check whose allow-list is **empty** is skipped, so an operator can enable the
checks one at a time.

### Header reference

| Header | Meaning | Example |
|--------|---------|---------|
| `X-Android-App` | the app's slug | `cinestream` |
| `X-Android-Platform` | must be `android` | `android` |
| `X-Android-Package` | the app's package name | `com.aistudio.cinestream.vqdpmr` |
| `X-Android-Installer` | package that installed the app | `com.android.vending` |
| `X-Android-Signature-Sha256` | signing-cert SHA-256, hex, colons optional | `AB:CD:…` |
| `X-Android-Tampered` | client's own tamper verdict | `1` |
| `X-Android-Debug` | debuggable build | `1` |
| `X-Android-Emulator` | emulator | `1` |
| `X-Android-Rooted` | rooted device | `1` |
| `X-Android-Build` | build identifier (already used) | `3.4.8.671` |
| `X-Android-Signature` | HMAC-SHA256 hex digest | `9f86d0…` |
| `X-Android-Timestamp` | unix seconds | `1763200000` |
| `X-Android-Nonce` | 8–128 chars, unique per request | `a1b2c3d4` |

Where the app may have come from, for `allowed_installers`:

| Installer package | Source |
|-------------------|--------|
| `com.android.vending` | Google Play Store |
| `com.google.android.packageinstaller` | sideloaded official signed APK |
| anything else | unknown — refused when the list is set |

### HMAC canonical string

```
METHOD \n
PATH   \n
TIMESTAMP \n
NONCE \n
SHA256(raw body, hex)
```

`PATH` is the request path only (no query string). The digest is
`HMAC-SHA256(secret, canonical)` as lowercase hex. A request is refused as
`stale_request` when `|now − timestamp| > 300s`, and as `replayed_request` when
the nonce was already seen inside that window (the nonce is cached for twice the
window).

---

## 3. Error format

Refusals are JSON, identical in shape to the rest of the app API:

```json
{
  "status": "error",
  "code": "installer_not_allowed",
  "message": "This app was not installed from an approved source."
}
```

| `code` | HTTP status |
|--------|-------------|
| `app_identity_mismatch`, `missing_platform_header`, `platform_not_android`, `browser_client`, `missing_package_header`, `package_mismatch`, `missing_installer_header`, `installer_not_allowed`, `missing_signature_header`, `signature_mismatch`, `tamper_detected`, `debug_build`, `emulator_detected`, `rooted_device` | 403 |
| `missing_request_signature`, `stale_request`, `replayed_request`, `invalid_request_signature` | 401 |
| `signature_not_configured` | 500 — an operator error: signing is required but no secret is set |

The message text lives in `REASONS` in `core/client_validation.py`; the app should
branch on `code`, never on `message`.

---

## 4. Observe mode versus enforce mode

`AndroidApp.strict_client_validation` decides whether a failure blocks:

* **off (default)** — *observe*. Every request is checked, a request that would
  have been refused is recorded with `would_block = 1`, and the request is
  **allowed through**. Nothing breaks.
* **on** — *enforce*. A failing request is refused with the code above.

This ordering exists because the checks read headers the shipped build does not
send yet. A deploy must never switch enforcement on by itself.

### Switching on safely

1. Open **Admin → Android Apps → APK Downloads** and confirm who may reach the
   download page (separate switches, section 6).
2. Open **Admin → Android Apps → &lt;app&gt; → Settings → Device checks**. Fill in
   the package name, installer list (Play, and the package installer if you hand
   out the APK) and the signing-certificate SHA-256. Leave *Enforce* unticked.
3. Ship an app build that sends the headers, then watch **Recent validation
   decisions** on the same page for a few days. The rows to look at are the
   **would block** ones: each is a real request from a real client that your
   strict rules would refuse, with the reason beside it.
4. When there are no unexplained *would block* rows, tick **Enforce these
   checks** and save. It applies on the next request; no restart.
5. Keep an eye on the same table afterwards. Refusals appear there with
   `decision = reject` and their reason code.

### Choosing the allow-lists

* `com.android.vending` covers Play installs. Add
  `com.google.android.packageinstaller` only if you also distribute the APK
  yourself; without it, every sideloaded copy is refused.
* The signing fingerprint is the one on the certificate that signs the build you
  ship. Changing signing keys means updating this list *before* the new build
  reaches users.
* Leave `allowed_package_names` and `allowed_signing_cert_sha256` set even if
  you leave emulator/root blocking off — package and certificate are the two
  claims that do the most work.

---

## 5. Logging

Every decision goes to `core.security` and, where it matters, to
`AppClientValidationLog`:

* **refusals** are always stored (`decision = reject`);
* in **observe mode**, a request that *would* be refused is stored as
  `decision = allow` with `would_block = 1` and its reason code;
* in **enforce mode**, successful requests are stored too (`decision = allow`),
  so the good traffic sits next to the bad;
* in observe mode a request that passes cleanly is **not** stored, so the table
  does not fill up with traffic that is already known to be fine.

Each row keeps what the client claimed next to what the server concluded:
`platform`, `package_name`, `installer`, `signature_sha256`, `build_identifier`,
`device_model`, `os_version`, plus `path`, `method`, `ip_address`, `user_agent`
and a `details` JSON blob. Rows are pruned by `AndroidApp.clean_old_analytics_data`
using `data_retention_days`, the same window as the app's other analytics.

Read them on **Admin → Android Apps → &lt;app&gt; → Settings → Recent validation
decisions** (last 15) or query `AppClientValidationLog` directly.

---

## 6. The download page (a separate, simpler control)

`/get-app/` — the Google Play install page — is governed by two site-wide
switches on `SiteSettings`, edited on **Admin → Android Apps → APK Downloads**:

| public | login required | Result |
|--------|----------------|--------|
| on | any | anyone may view it (public wins) |
| off | on | only signed-in visitors; others are sent to the sign-in screen |
| off | off | closed to everyone |

The answer is cached for five seconds per worker and the cache is cleared on
every `SiteSettings.save()`, so a change bites on the next request without a
restart. Only a real on/off value is accepted; anything else refuses the save
(`core/download_access.py::parse_toggle`).

---

## 7. Security practices for keeping this useful

1. **Never rely on this layer alone.** Keep the per-user JWT (`X-User-Token`)
   and the legacy cut-off (`legacy_user_auth_until`) in force; identity is what
   limits the damage of a copied credential.
2. **Rotate `access_password` and `request_signing_secret` when a build leaks.**
   Rotating the shared credential breaks every install, so prefer the per-app
   signing secret plus short token lifetimes.
3. **Treat the log as evidence, not as truth.** A row records what a client
   claimed. If the same IP shows a stable `signature_sha256` and package but
   changing device models every hour, that is a script, whatever the headers say.
4. **Turn on Google Play Integrity** as the next step. It is the only signal a
   client cannot forge; `expose_integrity_verdict` is the seam for it.
5. **Do not enable `rooted_device` blocking without measuring first** — plenty of
   ordinary users have a rooted device, and refusing them is a product decision,
   not a security one.
6. **Change one switch at a time.** Each allow-list is optional precisely so a
   failure can be attributed to the check that caused it.
7. **Watch the `would block` rows after every app release.** A new build that
   forgets a header shows up there before enforcement ever refuses a real user.