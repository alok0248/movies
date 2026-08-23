#!/bin/bash
# Deploy script for newmovies server
# Pulls latest code from GitHub and restarts the movies service
# Usage: ./deploy.sh

set -e

APP_DIR="/home/ubuntu/project/movies"
VENV_DIR="/home/ubuntu/project/movies/venv"
LOG_FILE="/home/ubuntu/deploy.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== Deploy started ==="

# Navigate to app directory
cd "$APP_DIR"

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Pull latest code
log "Pulling latest code..."
git fetch origin master
git reset --hard origin/master
log "Code updated to $(git rev-parse --short HEAD)"

# Run migrations
log "Running migrations..."
python manage.py migrate --noinput 2>&1 | tee -a "$LOG_FILE" || log "Migration warning (non-fatal)"

# Ensure nginx (www-data) can traverse directories to serve static files
chmod o+x /home 2>/dev/null || true
chmod o+x /home/ubuntu 2>/dev/null || true

# Collect static files
log "Collecting static files..."
python manage.py collectstatic --noinput 2>&1 | tee -a "$LOG_FILE" || true

# Restart gunicorn directly (no sudo needed)
log "Restarting gunicorn..."
pkill -f "gunicorn.*movie_portal.wsgi" 2>/dev/null || true
sleep 2
cd "$APP_DIR"
nohup "$VENV_DIR/bin/gunicorn" \
    movie_portal.wsgi:application \
    --bind 127.0.0.1:8000 \
    --workers 3 \
    --timeout 120 \
    --access-logfile /home/ubuntu/gunicorn-access.log \
    --error-logfile /home/ubuntu/gunicorn-error.log \
    --daemon
log "Gunicorn restarted (PID: $(pgrep -f 'gunicorn.*movie_portal.wsgi' | head -1))"

# Verify the site is up
sleep 3
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8000/" --max-time 10 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    log "✅ Deploy successful - site is up (HTTP $HTTP_CODE)"
else
    log "⚠️ Deploy completed but site returned HTTP $HTTP_CODE"
fi

log "=== Deploy finished ==="
