#!/usr/bin/env bash
# Apply deploy/ssl-renewal.md sections 1-6 on the server in one unattended run.
#
# Run it as the ubuntu user over ssh:   bash apply-ssl-renewal.sh
# Every privileged step goes through "sudo -n", so a missing passwordless rule
# stops the run at the first step instead of hanging on a prompt. The script is
# idempotent: the same files are installed, the nginx ACME block is added only
# when it is missing, and the lineage keeps the name newmovies.linkpc.net.
#
# It is section-by-section the runbook, including the checks that matter:
#   section 1  ACME path under /var/www/html proven before certbot is called
#   section 2  the three files installed root-owned with their required modes
#   section 3  certbot certonly, --dry-run first, then for real
#   section 4  lineage, renew_hook and the fingerprint nginx actually serves
#   section 5  the deployed certificate and the expiry alarm
#   section 6  the /etc/cron.d entry in and the broken user-crontab line out

set -u

DOMAIN=newmovies.linkpc.net
REPO=/home/ubuntu/project/movies
CHAIN=/etc/ssl/custom/${DOMAIN}_fullchain.cer
KEY=/etc/ssl/custom/${DOMAIN}.key
NGINX=/etc/nginx/sites-enabled/movies
CRON=/etc/cron.d/ssl-renewal
WEBROOT=/var/www/html
CHALLENGE=$WEBROOT/.well-known/acme-challenge
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/tmp/movies.nginx.$STAMP.bak
FAILED=0

say() { printf '\n=== %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }

fail() {
    printf '[FAIL] %s\n' "$*"
    exit 1
}

# ---------------------------------------------------------------- preconditions
say "preconditions"
id -u >/dev/null || fail "no id"
if ! sudo -n true 2>/dev/null; then
    fail "passwordless sudo is not available for this user; run the runbook by hand"
fi
echo "[OK] passwordless sudo available"
command -v certbot >/dev/null || fail "certbot is not installed"
echo "[OK] $(certbot --version 2>&1)"
for f in deploy/ssl/ssl_deploy_cert.sh deploy/ssl/ssl_auto_renew.sh deploy/ssl/ssl-renewal.cron; do
    [ -f "$REPO/$f" ] || fail "$REPO/$f is missing - pull the branch first"
done
echo "[OK] the three deploy/ssl files are present in $REPO"

# ------------------------------------------------- section 1: the ACME path
say "section 1: ACME challenge path"
sudo -n mkdir -p "$CHALLENGE" || fail "could not create $CHALLENGE"
echo "[OK] $CHALLENGE exists"

if grep -q 'well-known/acme-challenge' "$NGINX"; then
    echo "[OK] the ACME location is already in $NGINX"
else
    cp -p "$NGINX" "$BACKUP" || fail "could not back up $NGINX"
    echo "[OK] backed up $NGINX to $BACKUP"
    # The dedicated port-80 block owns the name, so the bare listen 80; in the
    # newmovies.linkpc.net block has to go (a server block cannot share port 80
    # for the same name). The default_server line is not touched.
    # Built in /tmp and copied over the file: sed -i cannot create its temporary
    # file in the root-owned sites-enabled directory, and the file itself is
    # writable by this user.
    NEW=/tmp/movies.nginx.new.$STAMP
    awk '!/^[[:space:]]*listen 80;[[:space:]]*$/' "$NGINX" > "$NEW" || fail "could not build the new config"
    cat >> "$NEW" <<'NGINX_EOF'

# ACME http-01 challenge files, served from /var/www/html so certbot --webroot
# can validate without reaching Django; everything else on port 80 redirects.
server {
    listen 80;
    server_name newmovies.linkpc.net;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
NGINX_EOF
    # exactly one bare "listen 80;" is right: the one in the block just appended
    # (the removed line is gone, and the default_server line never matched).
    if grep -q 'well-known/acme-challenge' "$NEW" && \
       [ "$(grep -c '^[[:space:]]*listen 80;[[:space:]]*$' "$NEW")" = "1" ]; then
        cat "$NEW" > "$NGINX" || fail "could not write $NGINX"
        echo "[OK] added the ACME block and removed the bare listen 80; line"
    else
        fail "the rewritten config is not what was intended; $NGINX left untouched"
    fi
fi

if sudo -n nginx -t >/tmp/nginx-t.$STAMP.out 2>&1; then
    echo "[OK] nginx -t accepted the configuration"
    sudo -n systemctl reload nginx || fail "nginx reload failed"
    echo "[OK] nginx reloaded"
else
    cat /tmp/nginx-t.$STAMP.out
    if [ -f "$BACKUP" ]; then
        cp -p "$BACKUP" "$NGINX"
        echo "[RESTORED] $NGINX restored from $BACKUP"
    fi
    fail "nginx -t rejected the configuration"
fi

echo precheck-ok >/tmp/precheck.$STAMP
# Bounded retry: right after a reload the old workers can still accept the odd
# connection and answer from the previous config, so a single probe is racy.
BODY=""
CODE=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    sudo -n install -m 0644 /tmp/precheck.$STAMP "$CHALLENGE/precheck" || fail "could not write the precheck file"
    BODY=$(curl -s --max-time 20 "http://$DOMAIN/.well-known/acme-challenge/precheck")
    sudo -n rm -f "$CHALLENGE/precheck"
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "http://$DOMAIN/.well-known/acme-challenge/precheck")
    if [ "$BODY" = "precheck-ok" ] && [ "$CODE" = "404" ]; then
        break
    fi
    echo "  attempt $attempt: body '$BODY', then $CODE - retrying"
    sleep 2
done
if [ "$BODY" = "precheck-ok" ] && [ "$CODE" = "404" ]; then
    echo "[OK] the challenge URL served the file from disk (body '$BODY'), now $CODE"
else
    fail "the challenge URL served '$BODY' and now answers $CODE - expected precheck-ok and 404"
fi

# ------------------------------------------- section 2: install the three files
say "section 2: install the three files"
sudo -n install -m 0755 "$REPO/deploy/ssl/ssl_deploy_cert.sh" /usr/local/bin/ssl_deploy_cert.sh || fail "deploy hook install failed"
sudo -n install -m 0755 "$REPO/deploy/ssl/ssl_auto_renew.sh" /usr/local/bin/ssl_auto_renew.sh || fail "renewal script install failed"
sudo -n install -m 0644 "$REPO/deploy/ssl/ssl-renewal.cron" "$CRON" || fail "cron file install failed"
# cron silently ignores a /etc/cron.d file with no trailing newline
if [ "$(sudo -n tail -c 1 "$CRON" | od -An -c | tr -d ' ')" != '\n' ]; then
    printf '\n' | sudo -n tee -a "$CRON" >/dev/null
    echo "[OK] added the missing trailing newline to $CRON"
fi
sudo -n ls -l /usr/local/bin/ssl_deploy_cert.sh /usr/local/bin/ssl_auto_renew.sh "$CRON"
ls -A /etc/letsencrypt/renewal-hooks/deploy/ | grep -q . \
    && warn "renewal-hooks/deploy is not empty; a second hook there would run a second time"

# ------------------------------------------------ section 3: issue the lineage
say "section 3: certbot dry run"
if sudo -n certbot certonly --dry-run \
        --webroot -w "$WEBROOT" \
        -d "$DOMAIN" --cert-name "$DOMAIN" \
        --deploy-hook /usr/local/bin/ssl_deploy_cert.sh \
        --non-interactive --agree-tos --register-unsafely-without-email; then
    echo "[OK] dry run succeeded (no deploy hook ran, /etc/ssl/custom untouched)"
else
    fail "the dry run failed - fix that before the real issuance"
fi

say "section 3: issue for real"
if sudo -n certbot certonly \
        --webroot -w "$WEBROOT" \
        -d "$DOMAIN" --cert-name "$DOMAIN" \
        --deploy-hook /usr/local/bin/ssl_deploy_cert.sh \
        --non-interactive --agree-tos --register-unsafely-without-email; then
    echo "[OK] the lineage exists and the deploy hook has run"
else
    fail "issuance failed"
fi

# ------------------------------------------------ section 4: verify the install
say "section 4: the lineage and the deploy hook"
sudo -n certbot certificates 2>/dev/null | sed -n '/Certificate Name/,/Private Key Path/p'
sudo -n grep -E '^(authenticator|webroot_path|renew_hook|key_type)' \
    "/etc/letsencrypt/renewal/$DOMAIN.conf" || warn "could not read the renewal config"
if ! sudo -n grep -q '^renew_hook' "/etc/letsencrypt/renewal/$DOMAIN.conf"; then
    warn "renew_hook is missing: a renewal would succeed and never reach /etc/ssl/custom"
    FAILED=1
fi

say "section 4: the pair nginx serves"
sudo -n ls -l /etc/ssl/custom/
ON_DISK=$(sudo -n openssl x509 -in "$CHAIN" -noout -fingerprint -sha256 | cut -d= -f2)
SERVED=$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha256 | cut -d= -f2)
echo "on disk : $ON_DISK"
echo "served  : $SERVED"
if [ -n "$ON_DISK" ] && [ "$ON_DISK" = "$SERVED" ]; then
    echo "[OK] nginx is serving the newly installed chain"
else
    warn "the served certificate is not the installed one"
    FAILED=1
fi

# --------------------------------------------------------- section 6: cron
say "section 6: cron, in and out"
echo "before: $(crontab -l | grep -c ssl_auto_renew) broken user-crontab line(s)"
crontab -l | grep -v ssl_auto_renew | crontab -
echo "after : $(crontab -l | grep -c ssl_auto_renew) broken user-crontab line(s)"
sudo -n ls -l "$CRON"
sudo -n tail -1 "$CRON"
echo -n "trailing newline: "
sudo -n tail -c 1 "$CRON" | od -An -c | tr -d ' \n'; echo

echo
say "section 6: the root entry run by hand"
if sudo -n /usr/local/bin/ssl_auto_renew.sh; then
    echo "[OK] ssl_auto_renew.sh exited 0"
else
    warn "ssl_auto_renew.sh did not exit 0"
    FAILED=1
fi
echo "--- /var/log/ssl_renewal.log"; sudo -n tail -3 /var/log/ssl_renewal.log 2>&1
echo "--- journalctl -t ssl-renew"; sudo -n journalctl -t ssl-renew -n 3 --no-pager 2>&1 | tail -3

# --------------------------------------------------------- section 5: alarm
say "section 5: what is served now, and the alarm"
echo -n "installed chain: "; sudo -n openssl x509 -in "$CHAIN" -noout -enddate
echo -n "served handshake: "
echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -enddate
echo
echo "--- manage.py check_health --no-send"
cd "$REPO" || fail "cannot cd to $REPO"
if ./venv/bin/python manage.py check_health --no-send; then
    echo "[OK] check_health exited 0"
else
    echo "[PROBLEM] check_health exited $?"
    FAILED=1
fi

say "result"
if [ "$FAILED" = "0" ]; then
    echo "all steps completed and verified"
else
    echo "steps completed with warnings above - read the [WARN]/[PROBLEM] lines"
fi
echo "nginx config backup for this run (only if it was patched): $BACKUP"
exit "$FAILED"
