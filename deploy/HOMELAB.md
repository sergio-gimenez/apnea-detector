# Homelab deployment

Recommended shape: one dedicated Debian 13 LXC on the `teruel` cluster, native Python
service, no Docker nesting. Suggested starting allocation: 2 vCPU, 2 GiB RAM, 32 GiB
root disk. Eight hours of PCM audio consumes about 0.92 GB before derived data or backups,
so increase storage before keeping many nights.

The workstation's `homelab` WireGuard interface was absent during the 2026-08-28 build,
and live SSH inspection timed out. Recheck free VMID, IP, storage, and current tunnel state
before creating anything. Do not reuse an address from the 2026-08-10 inventory snapshot.

## Install inside LXC

Transfer this repository into the new LXC, then run from repository root:

```sh
sudo ./deploy/install-lxc.sh
systemctl status apnea-detector
curl http://127.0.0.1:8080/api/health
```

Installer creates:

- Service user `apnea-detector`
- Versioned code and venv under `/opt/apnea-detector/releases`, atomically selected by `current`
- Sensitive data under `/var/lib/apnea-detector` (SQLite DB holds the password hash, TOTP
  secret, sessions, and device-token hashes; protected by the `0750` directory)
- `/etc/apnea-detector.env`, mode `0600`, for optional overrides only (no secrets required)
- Hardened `apnea-detector.service`, listening on `0.0.0.0:8080`

Rerun installer after transferring newer code to upgrade. It preserves the database (accounts
included), audio, and Garmin tokens.

## Operator account

No shared token. Create the account once, then finish MFA enrolment in the browser:

```sh
runuser -u apnea-detector -- env APNEA_DATA_DIR=/var/lib/apnea-detector \
  /opt/apnea-detector/current/venv/bin/apnea-admin create-user <username>
```

Same binary does `passwd`, `reset-mfa`, `list-users`, and `mint-token <username> --name phone`
to hand the recorder a revocable device token without the browser. The login rate limiter is
in-process, which is why the service must stay a single uvicorn worker (no `--workers`).

## Garmin login

Run login interactively inside LXC. Password and MFA go directly to Garmin; only OAuth
tokens persist under `/var/lib/apnea-detector/garmin`.

```sh
runuser -u apnea-detector -- env \
  APNEA_DATA_DIR=/var/lib/apnea-detector \
  GARMIN_TOKEN_STORE=/var/lib/apnea-detector/garmin \
  /opt/apnea-detector/current/venv/bin/apnea-garmin-login
```

## Internal hostname

Homelab internal ingress chain remains:

```text
sleep.home.sergiogimenez.com
  -> Pi-hole record pointing to 192.168.34.217
  -> Nginx Proxy Manager
  -> http://<new-lxc-ip>:8080
```

These are homelab mutations. Recheck live state, then add Pi-hole and NPM entries only
after explicit approval.

## Public hostname

Existing `sergiogimenez.com` connector is CT 114 according to inventory. Tunnel is remotely
managed, so add public hostname in Cloudflare dashboard, not a local `config.yml`:

```text
sleep.sergiogimenez.com -> http://<new-lxc-ip>:8080
```

Tunnel provides public TLS. Password + TOTP MFA protects every `/api/*` route except
`/api/health`; the static dashboard shell contains no data. The browser uses an `HttpOnly`
session cookie; the Android app uses a device token minted from the Security panel.

Because TLS terminates at the tunnel/NPM, the origin sees `http` while the browser's `Origin`
header is `https`. The CSRF check prefers `X-Forwarded-Proto` and otherwise falls back to a
host match, so it works as-is; add `APNEA_TRUSTED_ORIGINS=https://sleep.sergiogimenez.com` to
`/etc/apnea-detector.env` for an exact scheme+host match.

The login/MFA rate limiter buckets by client IP. Behind the proxy every request arrives from
the proxy's address, so also set `APNEA_TRUST_FORWARDED_FOR=1` **once you have confirmed the
origin port is not reachable except through NPM/Cloudflare** — otherwise a direct client could
forge `X-Forwarded-For` to dodge the per-IP lockout (the per-username ceiling still applies).

Cloudflare Access is now optional extra depth rather than the only barrier. If enabled for the
whole hostname, the Android app also needs an Access service token (`CF-Access-Client-Id` /
`CF-Access-Client-Secret`), which this APK does not yet send. Do not make the origin port
reachable from the internet directly.

## Backup and retention

Add new VMID to `pbs-macmini` nightly job only after confirming current PBS health. Inventory
reported recent backup job failures, so do not assume coverage.

Raw audio currently has no automatic retention policy. Delete expired session files and DB
records manually or destroy test LXC after experiment. Do not publish `/var/lib/apnea-detector`
through a file server.
