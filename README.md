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
- High-entropy bearer token protects sensitive API and audio routes for public tunnel use.

Deliberate prototype cuts: WAV instead of Opus, SQLite/local files instead of PostgreSQL/S3,
synchronous analysis instead of a queue, generic Garmin payload normalization, no automatic
retention, and no multi-user auth.

## Start backend

Create `.env` from `.env.example`, replace placeholder with output of
`openssl rand -hex 32`, then:

```sh
docker compose up -d --build
curl http://127.0.0.1:8080/api/health
```

Dashboard: `http://127.0.0.1:8080`. Browser requests API token on first data request.

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
2. Enter same API token configured on backend.
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
