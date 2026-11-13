#!/usr/bin/env bash
#
# /usr/local/bin/ssl_auto_renew.sh — the script the site's cron line has always
# pointed at, but which was never installed.
#
#   renew:   certbot renew --quiet — renews every lineage that is inside its
#            30-day window, and does nothing at all outside it, so this is safe
#            to run by hand at any time. Note: this certbot (1.21.0, the Ubuntu
#            package) has no --no-random-sleep-on-renew, so the flag must not be
#            used here — an unsupported flag aborts the whole run.
#   install: the deploy hook /usr/local/bin/ssl_deploy_cert.sh — recorded in the
#            lineage as renew_hook — copies the new chain into /etc/ssl/custom
#            and reloads nginx.
#   report:  notAfter of the file nginx actually serves, so the log says what is
#            live rather than only that certbot was happy.
#
# Must run as root: certbot writes /etc/letsencrypt and the challenge under
# /var/www/html, the hook writes /etc/ssl/custom and reloads nginx. The cron
# entry therefore lives in /etc/cron.d, not in a user's crontab.
#
# Exit status: certbot's, or 1 when the deployed certificate cannot be read
# (which also fails while the file does not exist yet).

set -u

CERT_NAME=${CERT_NAME:-newmovies.linkpc.net}
CERTBOT_BIN=${CERTBOT_BIN:-certbot}
CERT_FILE=${CERT_FILE:-/etc/ssl/custom/${CERT_NAME}_fullchain.cer}
LOG_TAG=ssl-renew

log() {
    printf '%s %s: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$LOG_TAG" "$*"
    if command -v logger >/dev/null 2>&1; then
        logger -t "$LOG_TAG" -- "$*"
    fi
}

if [ "$(id -u)" -ne 0 ]; then
    log "must run as root (uid $(id -u)); the root cron entry /etc/cron.d/ssl-renewal runs it"
    exit 1
fi

log "renewing: $("$CERTBOT_BIN" --version 2>&1 | head -1)"
"$CERTBOT_BIN" renew --quiet
status=$?
if [ "$status" -ne 0 ]; then
    log "certbot renew failed (exit $status) — see /var/log/letsencrypt/letsencrypt.log"
fi

if [ -r "$CERT_FILE" ]; then
    stamp=$(openssl x509 -in "$CERT_FILE" -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -n "$stamp" ]; then
        log "deployed certificate notAfter $stamp ($CERT_FILE)"
    else
        log "could not read $CERT_FILE as a certificate"
        [ "$status" -eq 0 ] && status=1
    fi
else
    log "$CERT_FILE is missing — nginx may have no certificate to serve"
    [ "$status" -eq 0 ] && status=1
fi

exit "$status"
