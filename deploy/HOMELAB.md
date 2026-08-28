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
- Sensitive data under `/var/lib/apnea-detector`
- Random bearer token in `/etc/apnea-detector.env`, mode `0600`
- Hardened `apnea-detector.service`, listening on `0.0.0.0:8080`

Rerun installer after transferring newer code to upgrade. It preserves database, audio,
Garmin tokens, and API token.

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

Tunnel provides public TLS. Backend bearer token still protects all `/api/*` routes except
`/api/health`; static dashboard shell contains no health data. Use random 64-hex token from
`/etc/apnea-detector.env` in Android app and browser prompt.

Cloudflare Access is optional defense in depth. If enabled for whole hostname, Android
also needs an Access service token (`CF-Access-Client-Id` and `CF-Access-Client-Secret`),
which this first APK does not yet support. For tonight, use backend bearer token and a
high-entropy unguessable value. Do not make origin port reachable from internet directly.

## Backup and retention

Add new VMID to `pbs-macmini` nightly job only after confirming current PBS health. Inventory
reported recent backup job failures, so do not assume coverage.

Raw audio currently has no automatic retention policy. Delete expired session files and DB
records manually or destroy test LXC after experiment. Do not publish `/var/lib/apnea-detector`
through a file server.
