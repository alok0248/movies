# Admin Interface Audit & Inventory

Baseline captured before any redesign work. Everything below was read from the
running code at `website/` (Django 5.x, `core` app). No behavioural change has
been made for this document.

- Route table: [core/urls.py](../core/urls.py)
- Views: [core/views.py](../core/views.py)
- Settings model: [core/models.py](../core/models.py#L247-L533)
- Admin chrome / navigation: [core/admin_base.html](../templates/core/admin_base.html#L1319-L1473)
- Settings hub groups: [core/views.py](../core/views.py#L18000-L18000) (`SETTINGS_HUB`)

---

## 1. Admin page inventory

All admin pages are staff-only (`@login_required` + `is_staff_or_superuser`) and
live under `/admin-dashboard/`. The sidebar exposes **37 top-level links** across
7 groups; most pages also have create/edit/delete/detail sub-pages.

### Dashboard
| Page | Route | View | Template | Depth |
|---|---|---|---|---|
| Overview | `/admin-dashboard/` | `admin_dashboard` | `admin_dashboard.html` | 1 |
| System | `/admin-dashboard/system-resources/` | `system_resource_dashboard` | `system_resource_dashboard.html` | 1 |
| DB Dashboard | `/admin-dashboard/settings/db-dashboard/` | `db_dashboard` | `db_dashboard.html` | 1 |
| Data Health | `/admin-dashboard/data-health/` | `data_health` | `data_health.html` | 1 |

### Traffic & Security
| Page | Route | View | Template | Depth |
|---|---|---|---|---|
| Health & Alerts | `/admin-dashboard/alerts/` | `admin_alerts` | `admin_alerts.html` | 1 |
| Requests & Users | `/admin-dashboard/requests/` | `admin_requests` | `admin_requests.html` | 1 |
| Device Activity | `/admin-dashboard/device-activity/` | `device_activity` | `admin_device_activity.html` | 1 |
| Devices & IPs | `/admin-dashboard/devices/` | `devices_ips` | `admin_devices_ips.html` | 1 |
| API Errors | `/admin-dashboard/api-errors/` | `api_error_log` | `admin_api_errors.html` | 1 |
| Audit Log | `/admin-dashboard/audit-log/` | `admin_audit_log` | `admin_audit_log.html` | 1 |

`Requests & Users` is a single page with 4 tabs (`?tab=users` Active Users,
`?tab=journeys` Journeys, `?tab=details` User Details, `?tab=log` Request Log).

### Content
| Page | Route | View | Template | Depth |
|---|---|---|---|---|
| Movies & TV | `/admin-dashboard/content/` | `admin_content_list` | `admin_content_list.html` | 1 |
| Content Rows | `/admin-dashboard/content-rows/` | `content_row_list` | `content_row_list.html` | 1 |
| Players | `/admin-dashboard/players/` | `player_list` | `player_list.html` | 1 |
| CinePlayer | `/admin-dashboard/cineplayer/` | `cineplayer_settings` | `cineplayer_settings.html` | 1 |
| Providers | `/admin-dashboard/providers/` | `provider_item_list` | `provider_item_list.html` | 1 |
| Watch Regions | `/admin-dashboard/watch-regions/` | `watch_region_list` | `watch_region_list.html` | 1 |

### Users
| Page | Route | View | Template | Depth |
|---|---|---|---|---|
| Users | `/admin-dashboard/users/` | `admin_user_list` | `admin_user_list.html` | 1 |
| Synced Users | `/admin-dashboard/synced-users/` | `synced_users_list` | `synced_users_list.html` | 1 |
| Notifications | `/admin-dashboard/notifications/` | `notifications_admin` | `admin_notifications.html` | 1 |
| User Messages | `/admin-dashboard/user-messages/` | `user_messages_admin` | `admin_user_messages.html` | 1 |
| History Calendar | `/admin-dashboard/history-calendar/` | `admin_history_calendar` | `admin_history_calendar.html` | 1 |

### Analytics
| Page | Route | View | Template | Depth |
|---|---|---|---|---|
| User Analytics | `/admin-dashboard/analytics/` | `admin_analytics` | `admin_analytics.html` | 1 |
| Engagement | `/admin-dashboard/analytics/engagement/` | `admin_engagement_analytics` | `admin_engagement_analytics.html` | 1 |
| Cohorts & Funnels | `/admin-dashboard/analytics/cohorts/` | `admin_retention_analytics` | `admin_retention_analytics.html` | 1 |
| Web Analytics | `/admin-dashboard/web-management/` | `web_management_dashboard` | `web_management_dashboard.html` | 1 |

### Android & API
| Page | Route | View | Template | Depth |
|---|---|---|---|---|
| Android Apps | `/admin-dashboard/android-apps/` | `android_app_list` | `android_app_list.html` | 1 |
| APK Downloads | `/admin-dashboard/android-apps/downloads/` | `android_app_downloads` | `android_app_downloads.html` | 1 |
| Android Analytics | `/admin-dashboard/android-apps/analytics/` | `android_app_dashboard` | `android_app_dashboard.html` | 1 |
| Ads | `/admin-dashboard/ads/` | `ad_list` | `ad_list.html` | 1 |
| Navbar | `/admin-dashboard/navbar-items/` | `navbar_item_list` | `navbar_item_list.html` | 1 |
| API Docs | `/admin-dashboard/api-docs/` | `api_documentation_view` | `api_documentation.html` | 1 |

### Settings
| Page | Route | View | Template | Depth |
|---|---|---|---|---|
| Settings Home | `/admin-dashboard/settings/` | `settings_root` → `settings_hub('overview')` | `settings_overview.html` | 1 |
| Site | `/admin-dashboard/settings/site/` | `settings_hub` | `settings_hub.html` | 1 |
| Data & APIs | `/admin-dashboard/settings/data/` | `settings_hub` | `settings_hub.html` | 1 |
| Delivery | `/admin-dashboard/settings/delivery/` | `settings_hub` | `settings_hub.html` | 1 |
| Security | `/admin-dashboard/settings/security/` | `settings_hub` | `settings_hub.html` | 1 |
| Data & Retention | `/admin-dashboard/data-volume/` | `admin_data_volume` | `admin_data_volume.html` | 1 |

The four hubs render one panel at a time (`?tab=`). Panel tabs today:

- `SETTINGS_HUB['site']` → `general`, `branding`, `footer`, `display`
- `SETTINGS_HUB['data']` → `source`, `tmdb`, `keys`, `usage`, `db`, `routing`
- `SETTINGS_HUB['delivery']` → `player`, `email`
- `SETTINGS_HUB['security']` → `protection`

Each tab maps to a **legacy standalone URL** that still resolves (bookmarks keep
working): `/settings/general/`, `/settings/branding/`, `/settings/display/`,
`/settings/data-source/`, `/settings/footer/`, `/settings/data-source-config/`,
`/settings/data-source-usage-stats/`, `/settings/tmdb-db/`, `/settings/player/`,
`/settings/email/`, `/settings/security-protection/`, `/settings/db-connections/`,
`/settings/db-routing/`.

### Sub-pages (create / edit / delete / detail)
Multiple: `content-rows/{create,<id>/edit,<id>/delete}`, `players/{create,<id>/edit,<id>/delete,<id>/toggle}`,
`ads/{create,<id>/edit,<id>/delete,<id>/toggle,adsterra*}`, `navbar-items/{create,<id>/edit,<id>/delete}`,
`providers/{create,<id>/edit,<id>/delete,<id>/toggle,sync}`, `watch-regions/{create,<id>/edit,<id>/delete,<id>/toggle}`,
`android-apps/{create,<id>/edit,<id>/settings,<id>/delete,<id>/toggle,<id>/analytics,<id>/failed-attempts,<id>/users-full}`,
`synced-users/<id>/{,edit,subscription,delete,resend-verification,block,wipe-data}`,
`users/{add,<id>/,<id>/play-history,<id>/edit,<id>/block,<id>/resend-verification,<id>/subscription,bulk/*}`,
`content/{add,<type>/<id>/edit,<type>/<id>/delete}`, `notifications/<id>/{,toggle}`,
`history-calendar/{data,day}`, `db-connections/<pk>/`.

### AJAX / JSON endpoints (admin surface)
`ajax/dashboard-stats/`, `admin-dashboard/web-management/data/`,
`admin-dashboard/system-resources/data/`, `admin-dashboard/requests/<pk>/peek/`,
`admin-dashboard/synced-users/<id>/peek/`, `admin-dashboard/ip-geo/[...]`,
`admin-dashboard/android-apps/<id>/analytics/json/`, `.../analytics/device/<uid>/logs/`,
`.../analytics/app-logs*`, `.../analytics/clear/<data_type>/`,
`admin-dashboard/history-calendar/{data,day}`, `settings/email/logs/`,
`ajax/{ad-files,ad-tags}/*`, `ajax/test-db-connection-new/`, `ajax/write-db-config/`,
plus the older `ajax/*` mutation endpoints (api keys, navbar toggle, footer toggle,
data-source switch, `save-db-config`, `toggle-hide-live-tv`).

---

## 2. `SiteSettings` schema (the central settings table)

Singleton row `pk=1`. Read through `SiteSettings.get_settings()` (LocMem cache,
60 s; invalidated on `save()`).

| # | Column | Type | Default | Choices / notes |
|---|---|---|---|---|
| 1 | `data_source` | CharField(10) | `tmdb` | `tmdb` / `tmdb_db` / `local` / `xtream` |
| 2 | `items_per_row` | IntegerField | 3 | 1,2,3,4,6 |
| 3 | `card_size` | CharField(10) | `medium` | small/medium/large |
| 4 | `title_size` | CharField(10) | `medium` | small/medium/large/xl |
| 5 | `text_size` | CharField(10) | `medium` | small/medium/large/xl |
| 6 | `theme_style` | CharField(20) | `cinevault` | 6 themes |
| 7 | `font_family` | CharField(50) | `system-ui` | 7 fonts |
| 8 | `brand_name` | CharField(50) | `newmovies` | |
| 9 | `brand_tagline` | CharField(200) | … | |
| 10 | `brand_color` | CharField(20) | `#00c896` | |
| 11–24 | `footer_enabled`, `footer_title`, `footer_description`, `footer_bottom_text`, `footer_links_title`, `footer_links`, `footer_genres_title`, `footer_genres`, `footer_countries_title`, `footer_countries`, `footer_subscribe_title`, `footer_subscribe_text`, `footer_subscribe_placeholder`, `footer_subscribe_button_text`, `footer_logo_text`, `footer_logo_tagline`, `footer_copyright_text`, `footer_disclaimer_text` | mixed | see model | footer builder inputs |
| 25 | `email_host` | CharField(100) | `smtp.gmail.com` | applied by `EmailSettingsMiddleware` |
| 26 | `email_port` | IntegerField | 587 | |
| 27 | `email_host_user` | EmailField | — | |
| 28 | `email_host_password` | CharField(200) | — | secret |
| 29 | `email_use_tls` | Boolean | True | |
| 30 | `watch_region` | CharField(10) | `US` | TMDB watch region |
| 31 | `curated_top_movie_ids` | TextField | — | CSV of TMDB ids |
| 32 | `curated_top_series_ids` | TextField | — | CSV of TMDB ids |
| 33 | `url_format` | CharField(10) | `slug` | slug/id |
| 34 | `tmdb_db_host` | CharField(255) | `localhost` | |
| 35 | `tmdb_db_port` | IntegerField | 5432 | |
| 36 | `tmdb_db_name` | CharField(255) | `tmdb` | |
| 37 | `tmdb_db_user` | CharField(255) | `tmdb` | |
| 38 | `tmdb_db_password` | CharField(255) | `tmdb123!` | secret |
| 39 | `tmdb_db_enabled` | Boolean | True | |
| 40 | `tmdb_db_enable_api_fallback` | Boolean | True | |
| 41 | `hide_live_tv` | Boolean | True | |
| 42 | `bot_ips` | TextField | — | consumed by `middleware.is_bot_request` |
| 43 | `bot_user_agents` | TextField | — | consumed by `middleware.is_bot_request` |
| 44 | `adsense_verification_meta` | CharField(200) | — | |
| 45 | `adsense_client_id` | CharField(100) | — | |
| 46 | `google_analytics_id` | CharField(50) | — | |
| 47 | `google_login_enabled` | Boolean | True | |
| 48 | `google_login_client_id` | CharField(200) | — | |
| 49 | `google_login_allowed_domains` | TextField | — | |
| 50 | `auto_click_every_clicks` | IntegerField | 10 | |
| 51 | `default_clicks_required` | IntegerField | 0 | |
| 52 | `require_ad_consent` | Boolean | False | |
| 53 | `ad_consent_message` | TextField | (copy) | |
| 54 | `max_ad_load_retries` | IntegerField | 3 | |
| 55 | `adsterra_clicks_required` | IntegerField | 5 | |
| 56 | `adsterra_show_banner` | Boolean | True | |
| 57 | `enable_adsterra_links` | Boolean | True | |
| 58 | `enable_autoclick_ads` | Boolean | True | |
| 59 | `idm_visibility` | CharField(20) | `all_users` | hide/logged_in/admin_only/all_users |
| 60 | `tile_ad_every_n` | IntegerField | 10 | |
| 61 | `enable_tile_click_gating` | Boolean | True | |
| 62 | `tile_ad_source` | CharField(20) | `ads_table` | ads_table/amazon_random/amazon_sequence |
| 63 | `tile_gating_source` | CharField(20) | `autoclick_urls` | autoclick_urls/amazon_random/amazon_sequence |
| 64 | `dev_mode_protection` | Boolean | False | enforced client-side in `base.html` |
| 65 | `require_login_to_browse` | Boolean | False | enforced by `LoginRequiredMiddleware` |
| 66 | `app_download_public_access` | Boolean | True | read by `core/download_access.py` |
| 67 | `app_download_login_required` | Boolean | False | read by `core/download_access.py` |
| 68 | `request_log_max_rows` | PositiveInteger | 5000 | read by `core/request_log.py` |
| 69 | `request_log_prune_every` | PositiveInteger | 200 | read by `core/request_log.py` |

Removed in this cleanup round: `allowed_ips`, `enable_url_blocking`,
`blocked_urls`, `redirect_url` (URL-blocking feature) and the `BlockedIP` /
`BlockedHeader` models — see migration `0147_remove_blockedheader_unique_blocked_header_and_more.py`.

## 3. Supporting tables touched by admin pages (abridged)

`RequestLog`, `RequestLogPreset`, `WebsiteVisitor`, `WebsiteVisitorVisit`,
`UserPageView`, `UserHeartbeat`, `DeviceIPHistory`, `IPLocation`,
`DeviceOnlineState`, `DeviceOnlineDaily`, `ErrorLog`, `AdminAuditLog`,
`AlertSettings`, `AlertRecipient`, `AlertIncident`, `HealthCheckAck`,
`AndroidApp`, `AndroidAppDevice`, `AndroidAppLog`, `AndroidAppFailedAttempt`,
`ApkDownload`, `ClientValidationLog`, `SyncedUser`, `UserCloudData`, `PlayHistory`,
`UserSession`, `AccountDeletionOtp`, `EmailAddress`, `EmailTemplate`,
`EmailSendLog`, `Subscriber`, `UserMessage`, `AppNotification`,
`AppNotificationRecipient`, `Ad`, `AdsterraLink`, `AdNetworkTag`,
`AmazonAffiliateProduct`, `NavbarItem`, `ProviderItem`, `WatchRegion`,
`ContentRow`, `PlayerConfiguration`, `TMDBMovie`, `TMDBTV`, `TMDBApiKey`,
`DataSourceUsageLog`, `DbConnectionConfig`, `DbRoutingConfig`, `ScheduledJob`.

## 4. Navigation & click-depth analysis

Every sidebar entry is a direct top-level link, so **all 37 destinations are
reachable in 1 click** from any admin page. The depth problem is *inside* the
pages, not in the sidebar:

- **Settings**: Settings Home → group → tab → save is 3 clicks to reach a panel
  (`settings_hub` → tab), plus the 13 legacy standalone URLs duplicate the same
  panels. This is the main "redundant page" cluster.
- **Requests & Users** hides 4 full views behind tabs of one URL — the old
  `/analytics/active-users/` URL survives only as a redirect (`admin_active_users_redirect`).
- **Ads** spreads `ad_list` + `admin_adsterra` + `admin_ad_files` + toggles across
  several URLs; Navbar/Providers/Watch Regions each have their own list+CRUD.
- Legacy `/admin-dashboard/edit-settings/` (`edit_settings`) still exists.
- The sidebar has a live filter (`#sbSearch`) plus collapsible sections.

## 5. Existing security baseline (before the requested hardening)

- Middleware stack: `SecurityMiddleware`, `CsrfViewMiddleware`,
  `XFrameOptionsMiddleware`, `AuthenticationMiddleware`, plus custom
  `HumanVerificationMiddleware` (Turnstile gate), `EmailVerificationMiddleware`,
  `LoginRequiredMiddleware`, `ContentSecurityPolicyMiddleware`,
  `BrowserCacheMiddleware`, `ErrorMonitoringMiddleware`, `RequestLogMiddleware`.
- `DEBUG=False` by default; `SECURE_SSL_REDIRECT=True`, `SESSION_COOKIE_SECURE`,
  `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`, `SameSite=Lax`,
  HSTS 180 days, `SECURE_CONTENT_TYPE_NOSNIFF=True`, `X_FRAME_OPTIONS=SAMEORIGIN`.
- All admin views are `@login_required` + `is_staff_or_superuser`; Django ORM
  everywhere (no raw string SQL found in the admin path); CSRF token on every
  form. Bot protection: Turnstile + Nginx (to be configured) + Fail2Ban.
- **Gaps for the requested controls**: no MFA/2FA anywhere; no role model beyond
  `is_staff`/`is_superuser`; CSP is deliberately wide open (needed by players/ads);
  no rate limit on admin endpoints beyond Nginx.

## 6. Findings / consolidation opportunities

1. **13 legacy settings URLs duplicate ≤4 hubs** — candidates for consolidation
   behind redirects (bookmarks preserved).
2. **`admin_active_users_redirect`** exists only for old bookmarks.
3. **`edit_settings`** legacy page overlaps the hubs.
4. **Ads / Navbar / Providers / Watch Regions** are four near-identical
   list+CRUD surfaces; a shared table+drawer pattern would cut page count.
5. Footer has 18 columns surfaced through one panel; could group.
6. **No MFA, no granular roles** — the security ask needs new capability, not
   just configuration.
7. CSP `default-src *` is intentional (players/ads) — tightening it must be
   validated against every embed or it will break playback.

## 7. Verification baseline (post-cleanup, this round)

- `manage.py check` → 0 issues.
- `manage.py makemigrations --check --dry-run` → No changes detected.
- `manage.py test core` → **223 tests, OK** (0 failures, 0 errors).
