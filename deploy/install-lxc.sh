#!/bin/sh
set -eu

# `python3 -m venv` honours the caller's umask; a restrictive one (e.g. 077 set
# to protect a secret) would make the venv unreadable to the service user and the
# unit would fail with status=203/EXEC. Pin a sane umask regardless of the caller.
umask 022

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run as root inside the dedicated LXC." >&2
    exit 1
fi

unset CDPATH
SOURCE_DIR=$(cd -- "$(dirname -- "$0")/.." && pwd)

apt-get update
apt-get install -y python3 python3-venv

if ! id apnea-detector >/dev/null 2>&1; then
    useradd --system --home /var/lib/apnea-detector --shell /usr/sbin/nologin apnea-detector
fi

install -d -o apnea-detector -g apnea-detector -m 0750 /var/lib/apnea-detector
install -d -o root -g root -m 0755 /opt/apnea-detector/releases

RELEASE_NAME=$(date +%Y%m%d%H%M%S)-$$
RELEASE=/opt/apnea-detector/releases/$RELEASE_NAME
trap 'rm -rf "$RELEASE"' EXIT
install -d -o root -g root -m 0755 "$RELEASE/backend"
install -o root -g root -m 0644 "$SOURCE_DIR/backend/pyproject.toml" "$RELEASE/backend/pyproject.toml"
cp -a "$SOURCE_DIR/backend/apnea_api" "$RELEASE/backend/apnea_api"
rm -rf "$RELEASE/backend/apnea_api/__pycache__"

python3 -m venv "$RELEASE/venv"
"$RELEASE/venv/bin/pip" install --no-cache-dir "$RELEASE/backend"
# the service user only reads this tree; make traversal explicit no matter the umask
chmod -R a+rX "$RELEASE"
trap - EXIT

ln -s "releases/$RELEASE_NAME" "/opt/apnea-detector/.current-$RELEASE_NAME"
mv -Tf "/opt/apnea-detector/.current-$RELEASE_NAME" /opt/apnea-detector/current

if [ ! -f /etc/apnea-detector.env ]; then
    ( umask 077
      printf '%s\n' "# Optional overrides, e.g. APNEA_TRUSTED_ORIGINS=https://sleep.example.com" \
        > /etc/apnea-detector.env )
fi
chmod 0600 /etc/apnea-detector.env

install -o root -g root -m 0644 "$SOURCE_DIR/deploy/apnea-detector.service" /etc/systemd/system/apnea-detector.service
systemctl daemon-reload
systemctl enable apnea-detector.service
systemctl restart apnea-detector.service

ADMIN="runuser -u apnea-detector -- env APNEA_DATA_DIR=/var/lib/apnea-detector \
/opt/apnea-detector/current/venv/bin/apnea-admin"

printf '%s\n' "Installed. Health endpoint: http://127.0.0.1:8080/api/health"
if $ADMIN list-users 2>/dev/null | grep -q .; then
    printf '%s\n' "Operator account already present. Manage it with: apnea-admin ..."
else
    printf '%s\n' "No operator account yet. Create one, then sign in and scan the MFA QR:"
    printf '  %s\n' "$ADMIN create-user <username>"
fi
