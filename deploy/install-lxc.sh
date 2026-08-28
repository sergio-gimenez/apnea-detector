#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run as root inside the dedicated LXC." >&2
    exit 1
fi

unset CDPATH
SOURCE_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)

apt-get update
apt-get install -y openssl python3 python3-venv

if ! id apnea-detector >/dev/null 2>&1; then
    useradd --system --home /var/lib/apnea-detector --shell /usr/sbin/nologin apnea-detector
fi

install -d -o apnea-detector -g apnea-detector -m 0750 /var/lib/apnea-detector
install -d -o root -g root -m 0755 /opt/apnea-detector/releases

RELEASE_NAME=$(date +%Y%m%d%H%M%S)-$$
STAGING=/opt/apnea-detector/releases/.staging-$RELEASE_NAME
RELEASE=/opt/apnea-detector/releases/$RELEASE_NAME
trap 'rm -rf "$STAGING"' EXIT
install -d -o root -g root -m 0755 "$STAGING/backend"
install -o root -g root -m 0644 "$SOURCE_DIR/backend/pyproject.toml" "$STAGING/backend/pyproject.toml"
cp -a "$SOURCE_DIR/backend/apnea_api" "$STAGING/backend/apnea_api"
rm -rf "$STAGING/backend/apnea_api/__pycache__"

python3 -m venv "$STAGING/venv"
"$STAGING/venv/bin/pip" install --no-cache-dir "$STAGING/backend"
mv "$STAGING" "$RELEASE"
trap - EXIT

ln -s "releases/$RELEASE_NAME" "/opt/apnea-detector/.current-$RELEASE_NAME"
mv -Tf "/opt/apnea-detector/.current-$RELEASE_NAME" /opt/apnea-detector/current

if [ ! -f /etc/apnea-detector.env ]; then
    umask 077
    printf 'APNEA_API_TOKEN=%s\n' "$(openssl rand -hex 32)" > /etc/apnea-detector.env
fi
chmod 0600 /etc/apnea-detector.env

install -o root -g root -m 0644 "$SOURCE_DIR/deploy/apnea-detector.service" /etc/systemd/system/apnea-detector.service
systemctl daemon-reload
systemctl enable apnea-detector.service
systemctl restart apnea-detector.service

printf '%s\n' "Installed. Health endpoint: http://127.0.0.1:8080/api/health"
printf '%s\n' "API token remains in /etc/apnea-detector.env (mode 0600)."
