# Admin dashboard improvement — implementation plan

Scope agreed: all four areas (Overview, Requests & log analytics, Visual
consistency / partials, Sidebar / navigation). This document is the plan only —
no behaviour changes yet.

Line references were taken from the current tree.

---

## 0. Verified current state (what already exists)

| Area | Already there | Evidence |
|---|---|---|
| Overview | 8 KPI tiles, API-health table, refusal reasons / failing paths, server gauges, alerts, 14-day Chart.js traffic chart, devices summary, poster repair | [admin_dashboard.html](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_dashboard.html) |
| Overview view | ~390-line read-only context builder, every block `try/except`-wrapped | [views.py#L907-L1298](file:///e:/websites/movies_app_website_151.145.57.106/website/core/views.py#L907-L1298) |
| Request log | `category` (page/api/admin) at record + filter layer | [request_log.py#L69-L76](file:///e:/websites/movies_app_website_151.145.57.106/website/core/request_log.py#L69-L76), [filter_requests](file:///e:/websites/movies_app_website_151.145.57.106/website/core/request_log.py#L268-L341) |
| Request log UI | Category chips, CSV **and** JSON export, "slowest endpoints" table, per-row details drawer, payload column | [admin_request_log.html#L16-L18](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_request_log.html#L16-L18), [#L125-L132](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_request_log.html#L125-L132), [#L237-L247](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_request_log.html#L237-L247) |
| Design kit | Shared `adm-*` kit + tokens (`--adm-line`, `--adm-ring`, `--adm-r`, `--adm-gap`) | [admin_base.html#L801-L814](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_base.html#L801-L814) |
| Sidebar | 7 sections, 38 links, filter box, collapsible groups, badges, colour legend, SPA-style content swap, hover prefetch | [admin_base.html#L1311-L1491](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_base.html#L1311-L1491) |

**Correction to [ui-and-request-tracking-review.md](file:///e:/websites/movies_app_website_151.145.57.106/website/docs/ui-and-request-tracking-review.md) §5 items 2, 3 and 5:** the Page/API/Admin split, the slowest-endpoint table and CSV export are all already implemented. Remaining §5 gaps are items 1 (short history), 4 (no error trend) and 7 (retention knob).

**Dead code found:** `ajax_dashboard_stats` is defined and routed but referenced by **no** template ([urls.py#L21](file:///e:/websites/movies_app_website_151.145.57.106/website/core/urls.py#L21), [views.py#L1355](file:///e:/websites/movies_app_website_151.145.57.106/website/core/views.py#L1355)). It also shells out to Linux-only `df -h /`.

---

## A. Overview dashboard

**Goal:** turn the top of the page into the four numbers an operator checks first, refreshed without a manual reload.

### A1. Four-tile top row
Add one row above the existing KPI grid with: **online now**, **requests/min**, **error rate (5xx %)**, **p95 latency**.

Data sources already available, no new tables:
- online now — `WebsiteVisitor.last_seen_at` within N minutes (same model the view already counts today).
- requests/min — `RequestLog` count over the last 60 s.
- error rate — `five_xx_rate(1440)` from [health.py](file:///e:/websites/movies_app_website_151.145.57.106/website/core/health.py) (already called).
- p95 — `_p95_latency_ms(hours=24, limit=5000)` [views.py#L5441](file:///e:/websites/movies_app_website_151.145.57.106/website/core/views.py#L5441-L5458) (currently unused by the dashboard).

Files: [views.py](file:///e:/websites/movies_app_website_151.145.57.106/website/core/views.py) (add a `headline` dict to the context), [admin_dashboard.html](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_dashboard.html) (markup only). Reuse the existing `.adm-kpi` / `.adm-grid` classes — no new CSS.

### A2. Auto-refresh
The page currently has a manual `Refresh` link ([admin_dashboard.html#L21](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_dashboard.html#L21)).

Two options — pick one:
- **(preferred) Reuse `ajax_dashboard_stats`:** extend it to return the four headline numbers + the existing live stats, then poll it every 30 s and patch the tiles in place. Removes the dead code instead of leaving a second, unused endpoint.
- **Or delete `ajax_dashboard_stats`** and reload only the headline strip via a small partial.

Either way, delete the Linux-only `df` block inside it and use `psutil` (already a dependency, already used by the dashboard view).

### A3. Empty-state / degradation polish
Move the dashboard view's private helpers into small module-level functions (`_dashboard_headline(now)`, `_dashboard_android(now)`) so each carries its own `try/except`. Behaviour identical; the view drops from ~390 to ~120 lines and the tiles get individually testable.

**Risk:** low — read-only, additive markup.

---

## B. Requests & log analytics

The UI is ahead of the data layer here; work is mostly backend + one small chart.

### B1. Nightly rollup so history survives prunning (review §5.1)
`RequestLog` self-prunes to the newest 5,000 rows ([request_log.py#L36](file:///e:/websites/movies_app_website_151.145.57.106/website/core/request_log.py#L36), [prune()](file:///e:/websites/movies_app_website_151.145.57.106/website/core/request_log.py#L226-L237)), i.e. hours of 5xx/4xx history at this traffic.

- New model `RequestLogRollup` (`path`, `date`, `hour`, `category`, `hits`, `errors`, `avg_ms`, `p95_ms`) + migration.
- New management command (e.g. `rollup_request_log`) that aggregates the previous hour before pruning; add it to the scheduled-job registry ([core/ScheduledJob](file:///e:/websites/movies_app_website_151.145.57.106/website/core/models.py)).
- **Risk:** medium — touches models + retention. Needs a test that the rollup runs *before* prune and that `prune()` is unchanged for rows already rolled up.

### B2. Error trend on the log page (review §5.4)
Small per-hour 4xx/5xx sparkline on [admin_request_log.html](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_request_log.html), sourced from `RequestLog` for the last 24 h (and from the rollup for older windows once B1 ships). One grouped query, mirrors the histogram already on the page.

### B3. Retention knob for the request log (review §5.7)
`MAX_ROWS = 5000` and `_PRUNE_EVERY = 200` are hard-coded. Expose them on the existing **Data & Retention** page ([admin_data_volume.html](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_data_volume.html)) via `SiteSettings`, read at prune time. Keep the current values as defaults so behaviour is unchanged for anyone who does not set them.

**Risk:** low once B1 exists (rollups mean lowering `MAX_ROWS` no longer loses history).

Not planned (already done): category chips, CSV/JSON export, slowest-endpoints table, payload drawer.

---

## C. Visual consistency / shared partials

**Measured problem:** 60 templates under `templates/core/` each carry their own inline `<style>` block, and `adm-page-head` is hand-copied into 21 of them ([admin_dashboard.html#L8](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_dashboard.html#L8), [data_health.html#L7](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/data_health.html#L7), [db_dashboard.html#L5](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/db_dashboard.html#L5), …). The kit itself is shared; the drift is in the per-page additions. Existing partials to model on: [_adm_pager.html](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/_adm_pager.html), [_data_health_badge.html](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/_data_health_badge.html).

### C1. Two partials, nothing else
- `_adm_page_head.html` — icon + title + subtitle + right-hand action slot. Variables: `title`, `icon`, `sub`, optional `badge`.
- `_adm_stat_row.html` — the KPI tile row (`.adm-kpi` grid), fed a list of `{k, v, n, href, tone}`.

### C2. Migrate incrementally, highest-traffic first
Order: Overview → Requests & Users → Device Activity → Notifications → Health & Alerts → Traffic Control. One commit per page. A page can be reverted alone; output markup is equivalent, only the source is de-duplicated.

**Risk:** medium-low. The real hazard is the SPA content swap in [admin_base.html#L2061-L2154](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_base.html#L2061-L2154) — partials must not rely on load-time JS. Keep partials server-rendered markup only.

### C3. Retire the duplicate colours
Per-page `<style>` rules that hard-code `#1a1a2e` / `rgba(15,23,42,…)` should reference the tokens instead. There is already a bridging layer for inline-style overrides ([admin_base.html#L763-L793](file:///e:/websites/movies_app_website_151.145.57.106/website/templates/core/admin_base.html#L763-L793)); extend that pattern rather than editing 60 files by hand.

---

## D. Sidebar / navigation

**Current:** 7 sections, 38 links, with a filter box and collapsible groups already shipped — so this is now a *regrouping* task, not a rebuild.

Proposed 6 sections, ≤ 6 links each (all URLs preserved; low-frequency pages become tabs on their parent, the pattern `Requests & Users` already uses via `?tab=`):

| Section | Items |
|---|---|
| Dashboard | Overview, System, DB Dashboard, Data Health |
| Traffic & Security | Requests & Users (±122 users/journeys/details tabs), Device Activity, Devices & IPs, Traffic Control, API Errors |
| Content | Movies & TV, Content Rows, Players, CinePlayer, Providers, Watch Regions |
| Users | Users, Synced Users, Notifications, User Messages, History Calendar |
| Android & API | Android Apps (±analytics/downloads tabs), Ads, Navbar, API Docs |
| Settings | Settings Home, Site, Data & APIs, Delivery, Security, Data & Retention |

Moves: `Audit Log` → under Health & Alerts as a tab; `Health & Alerts` stays in Traffic & Security; `Analytics` pages (User Analytics, Engagement, Cohorts, Web Analytics) fold into Dashboard as tabs. Honours the `prefers-reduced-motion` guard and filter/prefetch hooks already in place.

**Risk:** low — `admin_base.html` only. Verify `request.resolver_match.url_name` active-state logic still matches after the move.

---

## Suggested order

| # | Work | Files | Risk | Depends on |
|---|---|---|---|---|
| 1 | Retire or wire up `ajax_dashboard_stats` | views.py, urls.py | Low | — |
| 2 | Overview four-tile row + auto-refresh | views.py, admin_dashboard.html | Low | 1 |
| 3 | `_adm_page_head` partial + migrate Overview | new partial, admin_dashboard.html | Low | 2 |
| 4 | Splitting the dashboard view into helpers | views.py | Low | 2 |
| 5 | Request-log rollup model + command | models.py, migration, new command | Medium | — |
| 6 | Error-trend sparkline + retention knob | admin_request_log.html, admin_data_volume.html | Low | 5 |
| 7 | Migrate remaining pages to the partials | 20 templates | Medium-low | 3 |
| 8 | Sidebar regroup to 6 sections | admin_base.html | Low | — |

## Verification (for every step)

- `python manage.py check` and `python manage.py makemigrations --check`.
- `python manage.py test core` (existing suites: `test_retention`, `test_db_maintenance`, `test_scheduled_jobs`, `test_support_desk`).
- `python manage.py smoke_test` where present.
- Render-check the touched admin URLs through Django's test client and assert 200 — the method the 12 Nov session used for its 16 URLs.
- For 1, 2 and 8 specifically: assert the sidebar link count, the tile count and the JSON keys of the refresh endpoint.
