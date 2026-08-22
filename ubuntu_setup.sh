#!/usr/bin/env bash
set -euo pipefail

APP_NAME="movies"
APP_DIR="/var/www/${APP_NAME}"
SERVICE_NAME="${APP_NAME}"
PYTHON_VERSION="python3"
GIT_REPO_URL="https://github.com/YOUR_USERNAME/YOUR_REPO.git"
GIT_BRANCH="main"
DOMAIN="example.com"
WWW_DOMAIN="www.example.com"
APP_USER="www-data"
APP_GROUP="www-data"
DJANGO_SETTINGS_MODULE="movie_portal.settings"
DJANGO_ENV="prod"
DJANGO_ALLOWED_HOSTS="${DOMAIN},${WWW_DOMAIN}"
DJANGO_SECRET_KEY="change-me-to-a-long-random-secret"
DJANGO_DEBUG="False"
FORCE_SQLITE="1"
TMDB_API_KEY=""
CODESPECTERS_API_KEY=""
EMAIL_HOST=""
EMAIL_PORT="587"
EMAIL_USE_TLS="True"
EMAIL_HOST_USER=""
EMAIL_HOST_PASSWORD=""
ADMIN_USERNAME="admin"
ADMIN_EMAIL="admin@${DOMAIN}"
ADMIN_PASSWORD="change-this-admin-password"
CERTBOT_EMAIL="admin@${DOMAIN}"
ENABLE_HTTPS="1"

if [[ "${EUID}" -ne 0 ]]; then
  echo "[ERROR] Run this script as root."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "[INFO] Updating apt packages..."
apt-get update
apt-get upgrade -y

echo "[INFO] Installing system packages..."
apt-get install -y \
  git \
  curl \
  nginx \
  certbot \
  python3-certbot-nginx \
  ${PYTHON_VERSION} \
  python3-venv \
  python3-pip \
  build-essential \
  libpq-dev \
  unixodbc-dev \
  pkg-config

mkdir -p /var/www

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "[INFO] Cloning repository..."
  rm -rf "${APP_DIR}"
  git clone -b "${GIT_BRANCH}" "${GIT_REPO_URL}" "${APP_DIR}"
else
  echo "[INFO] Repository exists. Pulling latest changes..."
  git -C "${APP_DIR}" fetch origin
  git -C "${APP_DIR}" checkout "${GIT_BRANCH}"
  git -C "${APP_DIR}" pull origin "${GIT_BRANCH}"
fi

cd "${APP_DIR}"

echo "[INFO] Creating virtual environment..."
${PYTHON_VERSION} -m venv .venv
source .venv/bin/activate

echo "[INFO] Upgrading pip..."
pip install --upgrade pip wheel setuptools

echo "[INFO] Installing Python requirements..."
pip install -r requirements.txt
pip install gunicorn

echo "[INFO] Writing environment file..."
cat > "${APP_DIR}/.env" <<EOF
DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}
DJANGO_ENV=${DJANGO_ENV}
DJANGO_ALLOWED_HOSTS=${DJANGO_ALLOWED_HOSTS}
DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
DJANGO_DEBUG=${DJANGO_DEBUG}
FORCE_SQLITE=${FORCE_SQLITE}
TMDB_API_KEY=${TMDB_API_KEY}
CODESPECTERS_API_KEY=${CODESPECTERS_API_KEY}
EMAIL_HOST=${EMAIL_HOST}
EMAIL_PORT=${EMAIL_PORT}
EMAIL_USE_TLS=${EMAIL_USE_TLS}
EMAIL_HOST_USER=${EMAIL_HOST_USER}
EMAIL_HOST_PASSWORD=${EMAIL_HOST_PASSWORD}
EOF

chmod 600 "${APP_DIR}/.env"

mkdir -p "${APP_DIR}/static" "${APP_DIR}/staticfiles" "${APP_DIR}/media" "${APP_DIR}/logs"

echo "[INFO] Applying database migrations..."
set -a
source "${APP_DIR}/.env"
set +a
python manage.py migrate --noinput

echo "[INFO] Collecting static files..."
python manage.py collectstatic --noinput

echo "[INFO] Creating admin user if missing..."
python manage.py shell <<PY
from django.contrib.auth import get_user_model
User = get_user_model()
username = "${ADMIN_USERNAME}"
email = "${ADMIN_EMAIL}"
password = "${ADMIN_PASSWORD}"
if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username, email, password)
    print("Created superuser:", username)
else:
    print("Superuser already exists:", username)
PY

echo "[INFO] Setting file ownership..."
chown -R ${APP_USER}:${APP_GROUP} "${APP_DIR}"

GUNICORN_SOCKET="/run/${SERVICE_NAME}.sock"
GUNICORN_SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "[INFO] Writing systemd service..."
cat > "${GUNICORN_SERVICE_FILE}" <<EOF
[Unit]
Description=Gunicorn for ${APP_NAME}
After=network.target

[Service]
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/gunicorn --workers 3 --bind unix:${GUNICORN_SOCKET} movie_portal.wsgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

NGINX_SITE_FILE="/etc/nginx/sites-available/${APP_NAME}"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/${APP_NAME}"

echo "[INFO] Writing nginx site configuration..."
cat > "${NGINX_SITE_FILE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN} ${WWW_DOMAIN};

    client_max_body_size 50M;

    location /static/ {
        alias ${APP_DIR}/staticfiles/;
    }

    location /media/ {
        alias ${APP_DIR}/media/;
    }

    location / {
        include proxy_params;
        proxy_pass http://unix:${GUNICORN_SOCKET};
    }
}
EOF

ln -sf "${NGINX_SITE_FILE}" "${NGINX_SITE_LINK}"
rm -f /etc/nginx/sites-enabled/default

echo "[INFO] Testing nginx configuration..."
nginx -t

echo "[INFO] Enabling and starting services..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
systemctl enable nginx
systemctl restart nginx

if [[ "${ENABLE_HTTPS}" == "1" ]]; then
  echo "[INFO] Requesting HTTPS certificates with certbot..."
  certbot --nginx \
    --non-interactive \
    --agree-tos \
    --email "${CERTBOT_EMAIL}" \
    -d "${DOMAIN}" \
    -d "${WWW_DOMAIN}" \
    --redirect
fi

echo "[INFO] Final service status:"
systemctl --no-pager --full status "${SERVICE_NAME}" || true
systemctl --no-pager --full status nginx || true

echo "[INFO] Setup complete."
echo "[INFO] Update these variables in the script before running in production:"
echo "       GIT_REPO_URL, GIT_BRANCH, DOMAIN, WWW_DOMAIN, DJANGO_SECRET_KEY, ADMIN_PASSWORD, CERTBOT_EMAIL"
