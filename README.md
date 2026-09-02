# Nocturne

Android-first prototype for recording overnight breathing audio, aligning it with Garmin
signals, detecting low-audio respiratory-event candidates, and reviewing evidence on one
timeline.

This is a screening research prototype. It does not diagnose sleep apnea. Its SREI metric
is not AHI or REI.

## Install with Obtainium

Add this URL in Obtainium:

```text
https://github.com/sergio-gimenez/apnea-detector
```

Obtainium discovers signed APKs from GitHub Releases and keeps package
`com.sergiogimenez.nocturne` updated. Release builds use one persistent signing certificate;
do not install a debug APK over an Obtainium release.

## Tonight's vertical slice

- Android foreground microphone service survives screen-off recording.
- 16 kHz mono PCM 16-bit audio, split into recoverable 60-second WAV chunks.
- Every chunk records UTC start, monotonic start, cumulative sample offset, and sample count.
- Incomplete `.wav.part` chunks are repaired after service restart.
- Upload is offline-first and idempotent; recording never depends on network.
- FastAPI stores metadata in SQLite and audio in local storage.
- Deterministic NumPy detector finds adaptive low-energy runs lasting 10–120 seconds.
- Candidate confidence adds recovery energy, SpO₂ drop, and heart-rate response when present.
- `python-garminconnect` pulls sleep, heart rate, Pulse Ox, and respiration payloads.
- Browser dashboard shows whole-night signals, candidate evidence, authenticated audio clips,
  and confirmed/rejected/uncertain review labels.
- One operator account: scrypt-hashed password, mandatory authenticator-app (TOTP) MFA,
  opaque `HttpOnly` cookie sessions, and separately revocable per-device API tokens for the
  recorder. All `/api/*` routes except `/api/health` require it.
- Per-night context: free-text notes plus normalized tags (`van`, `alcohol`, `with partner`,
  `sick`, …), autosaved from the dashboard, so nightly circumstances accumulate alongside the
  metrics.
- `GET /api/export` returns every night as one flat record — context plus every computed
  metric — as JSON, or `?fmt=csv` for a spreadsheet. That is the hand-off point for later
  correlation work once enough nights exist.

Deliberate prototype cuts: WAV instead of Opus, SQLite/local files instead of PostgreSQL/S3,
synchronous analysis instead of a queue, generic Garmin payload normalization, no automatic
retention, and a single operator account with no roles or self-service signup.

## Start backend

```sh
docker compose up -d --build
curl http://127.0.0.1:8080/api/health
docker compose exec api apnea-admin create-user <username>
```

Dashboard: `http://127.0.0.1:8080`. First load prompts for the password, then walks through
authenticator-app enrolment (scan the QR, confirm a code, save the recovery codes). After that
the dashboard opens. Manage device tokens and active sign-ins from the **Security** panel.

For a throwaway local run with authentication fully disabled, set `APNEA_ALLOW_INSECURE_DEV=1`
(see `.env.example`).

Authenticate Garmin inside persistent backend volume:

```sh
docker compose exec api apnea-garmin-login
```

Garmin integration uses unofficial read-only web endpoints through
[`cyberjunky/python-garminconnect`](https://github.com/cyberjunky/python-garminconnect).
It may break when Garmin changes private APIs. Credentials remain in login process; reusable
OAuth tokens persist in backend data volume.

## Build Android APK

```sh
cd android
./gradlew lintDebug assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Debug APKs are local-test only. Public releases are built and signed by
`.github/workflows/android-release.yml` whenever a `v*` tag is pushed.

In app:

1. Set backend URL, such as `https://sleep.sergiogimenez.com`.
2. Enter a **device token**: dashboard → Security → *Mint a device token*, or
   `apnea-admin mint-token <username> --name phone` on the server. Revoke it there if the
   phone is lost; the password is never on the device.
3. Start capture while app is foreground, grant microphone/notification permissions.
4. Lock screen and leave phone charging, microphone unobstructed, near sleeper.
5. Stop capture in morning.
6. Tap **Upload**. Upload resumes safely because chunk endpoint is idempotent.
7. Open dashboard, pull Garmin data, then run analysis again to fuse physiology.

Do a 2–3 minute bedside test before overnight use. Confirm notification remains visible,
at least two chunks appear, upload completes, and dashboard audio plays.

## Timestamp model

Session and each chunk store both wall UTC and Android monotonic time. Within each recording
segment, chunk start comes from audio frame position mapped to `AudioTimestamp.TIMEBASE_MONOTONIC`.
Sample index is authoritative within captured audio. If Android restarts recorder, next chunk
has fresh monotonic anchor, preserving observable gap rather than pretending continuity.

## Detector limitations

V0.1 looks only for long, unusually low 1-second RMS energy relative to that night's audio.
It does not yet estimate respiratory envelope, classify snoring, distinguish microphone
occlusion, or reject fan/partner/environment noise. Candidate output is expected to contain
false positives and false negatives. Human audio review is mandatory.

## Development

Backend checks:

```sh
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest
```

Homelab native-LXC runbook: [`deploy/HOMELAB.md`](deploy/HOMELAB.md).

## License

MIT. See [`LICENSE`](LICENSE).
