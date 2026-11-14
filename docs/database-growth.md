# Database growth: what runs weekly, and when to move off SQLite

The site keeps everything in one SQLite file (`db.sqlite3`) in WAL mode. That is
a deliberate choice at this size — one VM, one writer at a time, no network
round-trip per query — but it has a ceiling and a maintenance cost. This page
records both.

## What already runs

| When | Command | What it protects |
|---|---|---|
| every 5 min | `check_health` | 5xx rate, disk, workers, TLS expiry, backup freshness → email / Telegram |
| daily 02:30 | `backup_db --media` | consistent snapshots + rotation (+ off-box when `BACKUP_UPLOAD_CMD`/`BACKUP_UPLOAD_DIR` is set) |
| daily 04:05 | `expire_subscriptions --apply` | subscriptions end on time |
| weekly Sun 04:05 | `db_maintenance --vacuum` | `quick_check`, bloat report, compaction |
| weekly Sun 04:15 | `cleanup_orphans --delete` | rows whose parent is gone |
| weekly Sun 04:30 | `link_devices --apply` | install records joined to accounts |
| weekly Sun 04:40 | `sync_verification --apply` | verified flags follow the account |
| weekly Sun 04:50 | `verify_backup` | proves the newest snapshot opens and has its rows |
| weekly Sun 04:55 | `check_catalogue_links --limit 20` | dead streams hidden |
| monthly 05:00 | `prune_app_data --apply` | retention windows |
| monthly 06:00 | `restore_backup … --to /tmp/restore-check` | a restore actually works |

`db_maintenance` prints page size, page count, freelist and bloat, warns when
`busy_timeout` is below the 20 s the app configures, and exits non-zero when the
file fails its check — so a cron mailer or a monitoring wrapper sees it.

Connection settings that matter:

* `DATABASES['default']['OPTIONS']['timeout'] = 20` — a blocked writer waits 20 s
  before raising "database is locked".
* WAL + `synchronous=NORMAL` are applied per connection in `core/signals.py`.

## When to move to PostgreSQL

Move when **any** of these is true (measured, not guessed):

1. **Size** — `db.sqlite3` above **1.5 GB**, or the nightly snapshot takes longer
   than the backup window (02:30 → 04:00) to copy and compress.
2. **Write contention** — `database is locked` appears in the request log or
   gunicorn log more than a handful of times a week, or p95 write latency
   (heartbeats, sync, messages) is above **300 ms** at normal load.
3. **Concurrency** — more than **2 gunicorn workers** are needed to keep up, i.e.
   sustained CPU pressure with an idle database — or writes must happen from a
   second process/host (a worker, a cron that writes heavily).
4. **Bloat that comes back** — the freelist returns above **25%** within a week
   of a VACUUM, which means churn (not just growth) is the problem.
5. **A feature needs it** — full-text search, concurrent long transactions,
   read replicas, or per-row locking (`SELECT … FOR UPDATE`).

Until then: run `db_maintenance --vacuum` weekly, keep the retention jobs on, and
watch the bloat line it prints. A VACUUM needs free disk roughly the size of the
file, so check `df -h` before forcing one on a box that is above 80% used.