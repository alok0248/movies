#!/usr/bin/env bash
# Staging copy of the site: the same code on http://127.0.0.1:8001 with a copy of
# the production database, so a release can be smoke-tested before it replaces
# what visitors see. Nothing here touches the live service or the live database.
#
#   bash scripts/staging.sh sync      # fast-forward staging to origin and refresh its DB
#   bash scripts/staging.sh smoke     # run the smoke suite against staging
#   bash scripts/staging.sh start     # (re)start the staging gunicorn
#   bash scripts/staging.sh stop
#   bash scripts/staging.sh status
#
# The deploy script runs `sync` then `smoke` and only restarts production when
# both pass.
set -eu

PROD=${PROD:-/home/ubuntu/project/movies}
STAGE=${STAGE:-/home/ubuntu/project/staging}
HOST=${HOST:-newmovies.linkpc.net}
PORT=${PORT:-8001}
PY="$PROD/venv/bin/python"
GUNICORN="$PROD/venv/bin/gunicorn"

running() { pgrep -f "gunicorn .*:${PORT}" | head -1; }

sync_code() {
    if [ ! -d "$STAGE/.git" ]; then
        git clone --shared "$PROD" "$STAGE" >/dev/null 2>&1
        echo "staging: cloned"
    fi
    cd "$STAGE"
    git fetch origin -q
    git reset --hard origin/master -q
    echo "staging: at $(git log --oneline -1)"

    "$PY" - "$PROD/db.sqlite3" "$STAGE/db.sqlite3" <<'PY'
import os, sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
print("staging: database refreshed (%.1f MB)" % (os.path.getsize(sys.argv[2]) / 1048576))
PY

    ln -sfn "$PROD/media" "$STAGE/media"
    ln -sfn "$PROD/staticfiles" "$STAGE/staticfiles"

    # The staging copy of the database is refreshed from production every time,
    # so a schema change in the pulled code has to be applied here before the
    # smoke suite reads any new table.
    (cd "$STAGE" && "$PY" manage.py migrate --noinput >/dev/null 2>&1) || {
        echo "staging: migrations failed — see the output above" >&2
        return 1
    }
}

start() {
    if [ -n "$(running)" ]; then
        stop
    fi
    cd "$STAGE"
    nohup "$GUNICORN" movie_portal.wsgi:application --bind "127.0.0.1:${PORT}" \
        --workers 1 --threads 4 --timeout 120 \
        --access-logfile /tmp/staging_access.log --error-logfile /tmp/staging_error.log \
        >/dev/null 2>&1 &
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        if curl -fsS -o /dev/null -m 2 "http://127.0.0.1:${PORT}/admin/" -H "Host: ${HOST}"; then
            echo "staging: listening on 127.0.0.1:${PORT} (pid $(running))"
            return 0
        fi
    done
    echo "staging: did not come up — see /tmp/staging_error.log" >&2
    tail -5 /tmp/staging_error.log >&2 || true
    return 1
}

stop() {
    local pid
    pid=$(running || true)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
        sleep 1
    fi
    # workers are children of the arbiter; clear any that outlived it
    pgrep -f "gunicorn .*:${PORT}" | while read -r leftover; do kill "$leftover" 2>/dev/null || true; done
    echo "staging: stopped"
}

case "${1:-status}" in
    sync)    sync_code ;;
    start)   sync_code; start ;;
    stop)    stop ;;
    restart) sync_code; start ;;
    smoke)
        cd "$STAGE"
        "$PY" manage.py smoke_test --base-url "http://127.0.0.1:${PORT}" --host "$HOST"
        ;;
    status)
        pid=$(running || true)
        if [ -n "$pid" ]; then
            echo "staging: running on 127.0.0.1:${PORT} (pid ${pid})"
        else
            echo "staging: not running"
        fi
        [ -d "$STAGE/.git" ] && (cd "$STAGE" && git log --oneline -1) || echo "staging: not cloned yet"
        ;;
    *)
        echo "usage: $0 {sync|smoke|start|stop|restart|status}" >&2
        exit 2
        ;;
esac
