#!/usr/bin/env bash
#
# Certbot deploy hook: put a renewed certificate where nginx reads it.
#
# Installed as /usr/local/bin/ssl_deploy_cert.sh and named by --deploy-hook when
# the lineage is created. Certbot saves that flag as `renew_hook` in
# /etc/letsencrypt/renewal/newmovies.linkpc.net.conf, so the systemd timer's
# twice-daily `certbot renew`, the /etc/cron.d entry and a hand run all copy the
# new fullchain and key into /etc/ssl/custom and reload nginx — but only after
# `nginx -t` accepts the result.
#
# Deliberately not also copied into /etc/letsencrypt/renewal-hooks/deploy/: a
# first `certonly` runs only --deploy-hook and never looks in that directory
# (certbot 1.21 _internal/main.py:137), and on a later renewal both copies would
# run, because the de-duplication compares paths, not contents (hooks.py:190).
#
# Runs as root, which is what certbot's hooks are. The target paths are the two
# lines nginx actually reads (/etc/nginx/sites-enabled/movies lines 73-74).
#
# Exit status: 0 when nginx is serving the new certificate, when the files were
# already current, or when there was nothing to install; non-zero when the copy
# was refused and rolled back, which certbot reports as a failed hook (renew
# still succeeded, so the old certificate stays in place and keeps working).

set -u

CERT_NAME=${CERT_NAME:-newmovies.linkpc.net}
CERT_DIR=${CERT_DIR:-/etc/ssl/custom}
LINEAGE=${RENEWED_LINEAGE:-/etc/letsencrypt/live/$CERT_NAME}
FULLCHAIN_SRC=$LINEAGE/fullchain.pem
KEY_SRC=$LINEAGE/privkey.pem
FULLCHAIN_DST=$CERT_DIR/${CERT_NAME}_fullchain.cer
KEY_DST=$CERT_DIR/${CERT_NAME}.key
BACKUP_DIR=$CERT_DIR/previous
NGINX_BIN=${NGINX_BIN:-nginx}
RELOAD_CMD=${RELOAD_CMD:-systemctl reload nginx}

log() { printf '%s ssl-deploy: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

if [ ! -r "$FULLCHAIN_SRC" ] || [ ! -r "$KEY_SRC" ]; then
    log "no usable certificate in $LINEAGE — nothing to install"
    exit 0
fi

# `certbot renew --dry-run` runs deploy hooks with a certificate from the
# staging CA. Installing one would replace a trusted chain with an untrusted
# one, so refuse before touching anything; the dry run then proves the whole
# renewal path without changing what the site serves.
issuer=$(openssl x509 -in "$FULLCHAIN_SRC" -noout -issuer 2>/dev/null || true)
if printf '%s' "$issuer" | grep -qiE 'staging|fake'; then
    log "refusing to install a staging certificate ($issuer)"
    exit 0
fi
if [ -z "$issuer" ]; then
    log "could not read the issuer of $FULLCHAIN_SRC"
    exit 1
fi

if cmp -s "$FULLCHAIN_SRC" "$FULLCHAIN_DST" && cmp -s "$KEY_SRC" "$KEY_DST"; then
    log "$FULLCHAIN_DST is already current — not reloading nginx"
    exit 0
fi

# Keep the working pair so a rejected certificate can be put back by hand.
install -d -m 0700 "$BACKUP_DIR"
if [ -f "$FULLCHAIN_DST" ]; then
    cp -p "$FULLCHAIN_DST" "$BACKUP_DIR/fullchain.cer.old"
fi
if [ -f "$KEY_DST" ]; then
    cp -p "$KEY_DST" "$BACKUP_DIR/$CERT_NAME.key.old"
fi

owner=()
if [ "$(id -u)" -eq 0 ]; then
    owner=(-o root -g root)
fi

# Write beside the target and rename: nginx never opens a half-written file, and
# the key ends up 0600 root:root — tighter than the ubuntu:ubuntu 0600 it
# replaces, and enough because nginx's master is the root process that opens it
# (`ps -o user= -C nginx`).
if ! install -m 0644 "${owner[@]}" "$FULLCHAIN_SRC" "$FULLCHAIN_DST.new" \
   || ! install -m 0600 "${owner[@]}" "$KEY_SRC" "$KEY_DST.new" \
   || ! mv -f "$FULLCHAIN_DST.new" "$FULLCHAIN_DST" \
   || ! mv -f "$KEY_DST.new" "$KEY_DST"; then
    log "could not write $FULLCHAIN_DST (or its key) — nginx is untouched"
    rm -f "$FULLCHAIN_DST.new" "$KEY_DST.new"
    exit 1
fi

if ! "$NGINX_BIN" -t; then
    log "nginx -t rejected the new certificate — restoring the previous one"
    if [ -f "$BACKUP_DIR/fullchain.cer.old" ]; then
        mv -f "$BACKUP_DIR/fullchain.cer.old" "$FULLCHAIN_DST"
    fi
    if [ -f "$BACKUP_DIR/$CERT_NAME.key.old" ]; then
        mv -f "$BACKUP_DIR/$CERT_NAME.key.old" "$KEY_DST"
    fi
    exit 1
fi

if ! $RELOAD_CMD; then
    log "nginx -t passed but '$RELOAD_CMD' failed — the new files are in place, nginx keeps serving the old certificate until it is reloaded"
    exit 1
fi

log "installed $FULLCHAIN_DST (notAfter $(openssl x509 -in "$FULLCHAIN_DST" -noout -enddate | cut -d= -f2)) and reloaded nginx"
exit 0
