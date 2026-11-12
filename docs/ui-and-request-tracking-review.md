# Website + App UI review — 12 Nov 2026

Scope: admin panel (35 sidebar items across 6 sections), public site base template
(`templates/base.html`, 4,900 lines), request logging (`core/request_log.py`,
`core/middleware.py`, `/admin-dashboard/requests/`), and the Android API layer.
Everything below is a proposal — nothing in this file changes behaviour by itself.

---

## 1. Navigation: 35 items in 6 sections is the main structural problem

Current sidebar:

| Section | Items |
|---|---|
| Overview | Overview, DB Dashboard, Data Health, User Analytics, Engagement, Web Analytics, System |
| Security & Health | Traffic Control, Requests & Users, Device Activity, Audit Log, Notifications, Health & Alerts, Data & Retention, API Errors |
| Content | Movies & TV, Content Rows, Players, CinePlayer, Providers, Watch Regions |
| Users & Access | Users, Synced Users, History Calendar |
| Android & API | Android Apps, APK Downloads, Android Analytics, Ads, Navbar, API Docs |
| Settings | Overview, Site, Data & APIs, Delivery, Security |

Four of the six sections mix "look at a number" pages with "change a setting" pages, and
two items appear twice by name (`Overview`, `Security`). Suggested merge to **5 groups of
≤ 6**, where the low-frequency items become tabs inside their parent page (the
`Requests & Users` page already does exactly this with `?tab=`, so the pattern exists):

1. **Dashboard** — Overview, System, DB Dashboard
2. **Traffic & Security** — Requests & Users (tabs: log / users / journeys), Device
   Activity, Traffic Control, Health & Alerts (tab: data health, API errors), Audit Log
3. **Content** — Movies & TV, Content Rows, Players & Providers, CinePlayer, Watch Regions
4. **Users** — Users, Synced Users, Subscriptions (from Settings), History Calendar
5. **Android** — Android Apps (tabs: analytics, APK downloads, ads), Notifications, API Docs
6. **Settings** — one page with tabs: Site, Data & APIs, Delivery, Security, Retention

Net effect: 35 links → ~22, no destination removed, every existing URL keeps working
(only the sidebar markup and two new tab strips change). The duplicate `Overview` /
`Security` labels are what makes the current list feel longer than it is.

**Also worth fixing while in there:** the sidebar has no search. With 20+ entries a
one-line filter box at the top pays for itself; the markup is already flat `<a>` items.

---

## 2. Page look: inconsistency is what reads as "not polished"

The admin pages each carry their own `{{ ... }}` colour values in an inline `<style>`
block (the new Notifications / Device Activity pages I added follow that pattern too).
Measured inconsistencies:

- **Card backgrounds** vary between `#1a1a2e`, `rgba(15,23,42,.7)` and `rgba(15,23,42,.6)`
  on the same screen; borders between `rgba(148,163,184,.18)` and `.2`.
- **Page headers** are sometimes an icon + title + subtitle row, sometimes just an `<h2>`.
- **Number formatting** is inconsistent: some pages print raw integers, others abbreviate.
- **Tables** differ in row padding (`.4rem` to `.48rem`) and header casing.

Concrete fix, low risk: define the eight tokens once in `admin_base.html`
(`--card`, `--card-2`, `--line`, `--line-soft`, `--text`, `--text-2`, `--text-3`,
`--accent`), then add three shared partials — `_page_header.html`,
`_stat_row.html`, `_table.html` — and migrate pages one at a time. Behaviour is
untouched; only class names and inline colours change, so a page can be reverted alone
if it looks worse.

Suggested visual direction (keeps the current dark identity): one accent for
navigation/selection (sky), one for "act now" (amber), one for success (green), and
reserve red strictly for errors/destructive actions. Right now amber is used for both
"attention" and "brand accent", which is why focus rings and warnings look alike.

### Per-page polish, highest impact first
1. **Overview** — the only page a new operator sees first. Add a 4-tile top row
   (online now, requests/min, error rate, slowest endpoint p95) above the existing cards.
2. **Requests & Users** — hide the raw JSON payload column behind a per-row expand
   (progressive disclosure); keep method/status/path/duration visible.
3. **Device Activity** — the bar chart needs a y-axis scale and a "today" marker;
   without a scale the bars cannot be compared across devices.
4. **Notifications** — show the audience size *before* sending ("will reach 14 users"),
   which is the one number you need at the moment of pressing Send.
5. **Empty states** — every list (requests, devices, notifications) should have a
   one-line explanation plus the action that fills it, not a bare "Nothing here".

---

## 3. "Buttery fluid" — website

Measured starting points:

- `templates/base.html` is **4,900 lines** and contains the whole public CSS in two
  inline `<style>` blocks → the page HTML is re-downloaded on every navigation and
  cannot be cached. Move the non-critical block to `static/css/site.css` (hashed
  filename), keep ~6 KB of critical CSS inline. This is the single biggest win.
- **Lazy loading is only used in 14 of the 28 templates that render `<img>`.** Add
  `loading="lazy"` + `decoding="async"` to everything below the fold, and always set
  `width`/`height` (or `aspect-ratio`) so the grid stops jumping as posters arrive —
  layout shift is what makes scrolling feel choppy, more than any animation.
- **`prefers-reduced-motion` is honoured in exactly one template.** If transitions are
  added, gate them: `@media (prefers-reduced-motion: no-preference) { ... }`.
- Bootstrap and the Google SDK load from CDNs without `defer`; the GTM `<script async>`
  is in `<head>`. Move the SDK call to the login/account page only, and add `defer` to
  the bundle.
- Cache headers: the request log shows API traffic but no `ETag`/`Cache-Control` on
  public HTML. Even `Cache-Control: private, max-age=60` on catalogue pages removes most
  repeat round-trips.
- Hover prefetch for admin sidebar links (`<link rel="prefetch">` on pointerenter) makes
  tab-switching feel instant at the cost of a few KB.

Avoid: scroll-jacking, long entrance animations, and parallax — they read as "slow"
on the TV-class hardware you also support.

---

## 4. "Buttery fluid" — app + API

The app is Compose with a very large `MediaViewModel` (15k lines) exposing dozens of
`StateFlow`s. Whole-screen `collectAsState()` on several of them is the usual cause of
scroll jank:

- Use `collectAsStateWithLifecycle()` and expose **derived** state for lists
  (`derivedStateOf`) so a heartbeat/position tick does not recompose poster grids.
- Keep `@Immutable` data classes for `MediaItem` and friends; otherwise Compose
  re-composes rows whose data did not change.
- OkHttp: share one `OkHttpClient` (connection pool + HTTP/2), enable a disk cache and
  send `If-None-Match`. Every app API currently opens a fresh request path per call.
- Add pagination + a small page size to catalogue endpoints; the heart of "buttery" is
  never downloading 500 rows to show 20.
- Coil: memory + disk cache with `crossfade(true)`, and prefetch the next row while the
  current one is on screen.
- Baseline profile (`baseline-prof.txt`) for startup; a TV box boots this app cold.
- Keep the 1-minute heartbeat (it is already cheap) but batch `recentActions` and never
  let it run while the app is backgrounded and idle.

API side: return `Cache-Control` + `ETag` on catalogue GETs, `gzip` for JSON, and keep
the new `/api/notifications/` `since` parameter as the model for other polling
endpoints — the pattern of "give me what changed" is what keeps mobile traffic small.

---

## 5. Request tracking: what already exists, and the real gaps

Already implemented and working:

- `RequestLogMiddleware` + `core/request_log.py` record **every** request (page, Ajax and
  Android API), with IP, method, path, status, duration, user agent and masked payloads
  (passwords/tokens/OTP never stored). Static/media/health paths are skipped, and the
  table self-prunes at 5,000 rows.
- `/admin-dashboard/requests/` has four tabs — request log, active users, visitor
  journeys, per-account details — with filters and pagination.

Gaps worth closing (in order):

1. **History is short by design.** 5,000 rows at your traffic is hours, not days. Add a
   nightly rollup table (`path`, `hour`, `hits`, `errors`, `avg_ms`, `p95_ms`) before
   pruning, so "yesterday" and "last week" survive.
2. **No API vs page split.** The log mixes `/api/...` and `/admin-dashboard/...`. One
   filter chip pair (`Page` / `API` / `Admin`) makes the page answer the question admin
   actually asks: "is the app slow or is the site slow?"
3. **No slowest-endpoint view.** A small "top 10 by p95" table (the `_p95` helper
   already exists in views.py) turns 5,000 rows into a decision.
4. **No error trend.** `API Errors` is a separate page; a sparkline of 4xx/5xx per hour
   on the request page would surface a regression the same day it ships.
5. **No export.** Add CSV for the current filter (the notification page already does
   this — same three lines of code).
6. **App-side correlation.** The app already reports via `RemoteLogManager`; include the
   `deviceId` in API error payloads so a 500 in the log can be tied to a device and app
   version without guessing from the user agent.
7. **Retention policy UI.** `Data & Retention` exists but the request log's own cap is
   hard-coded; expose it there (e.g. "keep 7 days of request rows").

---

## 6. Suggested order of work

| Priority | Change | Risk | Why first |
|---|---|---|---|
| P0 | Sidebar merge to 5–6 groups + sidebar search | Low (template only) | Biggest perceived improvement, no URL changes |
| P0 | Set `width`/`height` + `loading=lazy` on remaining images | Very low | Removes layout jump on every grid |
| P1 | Extract public CSS out of `base.html` | Medium (visual regression possible) | Page weight + caching |
| P1 | Shared page-header / stat-row partials, then migrate 5 busiest admin pages | Medium | Stops the "each page looks slightly different" effect |
| P1 | Request-log rollups + Page/API/Admin chips + CSV | Low | Makes the log usable for decisions, not just inspection |
| P2 | App: `collectAsStateWithLifecycle`, OkHttp cache, pagination | Medium (needs device testing) | Perceived speed on TV boxes |
| P2 | Prefetch admin links, reduced-motion guard, ETag on catalogue | Low | Polish |

I can take any row of that table as a self-contained change. The P0 items are the ones
that touch only markup and can be verified page-by-page in the local server without
touching the API or database.
