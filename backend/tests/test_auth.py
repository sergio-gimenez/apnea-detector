import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apnea_api import totp
from apnea_api.admin import create_user
from apnea_api.main import create_app
from apnea_api.models import TrustedDevice

USERNAME = "sergio"
PASSWORD = "correct horse battery staple"
ORIGIN = "https://testserver"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("APNEA_ALLOW_INSECURE_DEV", raising=False)
    db_url = f"sqlite:///{tmp_path / 'auth.db'}"
    application = create_app(tmp_path, db_url)
    with sessionmaker(create_engine(db_url))() as db:
        create_user(db, USERNAME, PASSWORD)
    return application


def client(app, *, origin=True):
    headers = {"Origin": ORIGIN} if origin else {}
    return TestClient(app, base_url=ORIGIN, headers=headers)


def login(c, password=PASSWORD):
    return c.post("/api/auth/login", json={"username": USERNAME, "password": password})


def enroll(c):
    """Complete first-time MFA setup; return (totp_secret, recovery_codes)."""
    assert login(c).json()["needs_enrollment"] is True
    secret = c.post("/api/auth/mfa/setup").json()["secret"]
    body = c.post("/api/auth/mfa/enable", json={"code": totp.now_code(secret)})
    assert body.status_code == 200
    return secret, body.json()["recovery_codes"]


def open_db(tmp_path):
    """A second connection to the app's database, for asserting on stored rows."""
    return sessionmaker(create_engine(f"sqlite:///{tmp_path / 'auth.db'}"))()


def sign_in(c, secret, *, remember=False, step=1):
    """Password plus a code, optionally remembering the browser.

    ``step`` picks a later TOTP step (still inside the server's ±1 window) so a
    second sign-in in the same test is not refused as a replayed code.
    """
    assert login(c).status_code == 200
    code = totp.now_code(secret, at=time.time() + step * totp.PERIOD)
    verified = c.post("/api/auth/mfa/verify", json={"code": code, "remember": remember})
    assert verified.status_code == 200
    return verified.json()


def test_data_routes_require_authentication(app):
    c = client(app)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/sessions").status_code == 401


def test_wrong_password_is_rejected_then_rate_limited(app):
    c = client(app)
    for _ in range(8):
        assert login(c, "wrong").status_code == 401
    assert login(c, "wrong").status_code == 429
    # lockout is by username+IP, so the correct password is refused too until it clears
    assert login(c).status_code == 429


def test_login_without_mfa_is_gated_until_enrolled(app):
    c = client(app)
    body = login(c).json()
    assert body == {
        "username": USERNAME,
        "mfa_enabled": False,
        "mfa_required": False,
        "needs_enrollment": True,
        "remembered": False,
    }
    gated = c.get("/api/sessions")
    assert gated.status_code == 403 and gated.json()["needs_mfa"] is True

    setup = c.post("/api/auth/mfa/setup").json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    assert setup["qr_data_uri"].startswith("data:image/svg+xml")

    enabled = c.post("/api/auth/mfa/enable", json={"code": totp.now_code(setup["secret"])})
    assert enabled.status_code == 200
    assert len(enabled.json()["recovery_codes"]) == 10

    assert c.get("/api/sessions").status_code == 200
    assert c.get("/api/auth/session").json() == {
        "username": USERNAME,
        "mfa_enabled": True,
        "mfa_required": False,
        "needs_enrollment": False,
        "remembered": False,
    }


def test_enrolled_account_needs_totp_on_a_fresh_client(app):
    secret, _ = enroll(client(app))

    c = client(app)
    body = login(c).json()
    assert body["mfa_required"] is True and body["mfa_enabled"] is True
    assert c.get("/api/sessions").status_code == 403

    bad = c.post("/api/auth/mfa/verify", json={"code": "000000"})
    assert bad.status_code == 401
    ok = c.post("/api/auth/mfa/verify", json={"code": totp.now_code(secret)})
    assert ok.status_code == 200
    assert c.get("/api/sessions").status_code == 200


def test_totp_code_cannot_be_replayed(app):
    secret, _ = enroll(client(app))
    code = totp.now_code(secret)

    a = client(app)
    login(a)
    assert a.post("/api/auth/mfa/verify", json={"code": code}).status_code == 200

    b = client(app)
    login(b)
    # same code, different session: the step has been spent
    assert b.post("/api/auth/mfa/verify", json={"code": code}).status_code == 401
    assert b.get("/api/sessions").status_code == 403


def test_session_id_rotates_when_mfa_is_satisfied(app):
    secret, _ = enroll(client(app))
    c = client(app)
    login(c)
    partial = c.cookies.get("__Host-nocturne_session")
    assert c.post("/api/auth/mfa/verify", json={"code": totp.now_code(secret)}).status_code == 200
    full = c.cookies.get("__Host-nocturne_session")
    assert partial and full and partial != full
    # the pre-MFA identifier is dead, not merely downgraded
    stale = TestClient(app, base_url=ORIGIN)
    got = stale.get("/api/auth/session", headers={"Cookie": f"__Host-nocturne_session={partial}"})
    assert got.status_code == 401


def test_unknown_user_and_wrong_password_are_indistinguishable(app):
    c = client(app)
    missing = c.post("/api/auth/login", json={"username": "nobody", "password": "x" * 12})
    wrong = c.post("/api/auth/login", json={"username": USERNAME, "password": "x" * 12})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["detail"] == wrong.json()["detail"]


def test_security_headers_block_framing_and_sniffing(app):
    r = client(app).get("/api/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_null_origin_is_refused_on_cookie_mutations(app):
    c = client(app)
    enroll(c)
    assert c.post("/api/auth/logout", headers={"Origin": "null"}).status_code == 403


def test_lockout_keys_on_cloudflare_ip_when_forwarding_is_trusted(tmp_path, monkeypatch):
    monkeypatch.delenv("APNEA_ALLOW_INSECURE_DEV", raising=False)
    monkeypatch.setenv("APNEA_TRUST_FORWARDED_FOR", "1")
    db_url = f"sqlite:///{tmp_path / 'xff.db'}"
    application = create_app(tmp_path, db_url)
    with sessionmaker(create_engine(db_url))() as db:
        create_user(db, USERNAME, PASSWORD)
    c = TestClient(application, base_url=ORIGIN, headers={"Origin": ORIGIN})

    attacker = {"CF-Connecting-IP": "203.0.113.9"}
    for _ in range(8):
        assert c.post(
            "/api/auth/login", json={"username": USERNAME, "password": "wrong"}, headers=attacker
        ).status_code == 401
    assert c.post(
        "/api/auth/login", json={"username": USERNAME, "password": "wrong"}, headers=attacker
    ).status_code == 429
    # a different edge client is not caught by the per-IP lock (the leftmost
    # X-Forwarded-For is ignored in favour of CF-Connecting-IP)
    victim = {"CF-Connecting-IP": "198.51.100.7", "X-Forwarded-For": "203.0.113.9"}
    assert c.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers=victim
    ).status_code == 200


def test_recovery_code_authenticates_once(app):
    _, codes = enroll(client(app))

    first = client(app)
    login(first)
    assert first.post("/api/auth/mfa/verify", json={"code": codes[0]}).status_code == 200
    assert first.get("/api/sessions").status_code == 200

    second = client(app)
    login(second)
    assert second.post("/api/auth/mfa/verify", json={"code": codes[0]}).status_code == 401
    assert second.post("/api/auth/mfa/verify", json={"code": codes[1]}).status_code == 200


def test_cookie_mutations_require_a_trusted_origin(app):
    c = client(app)
    enroll(c)
    assert c.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/auth/session").status_code == 200  # still signed in
    assert c.post("/api/auth/logout").status_code == 200


def test_device_token_authenticates_without_cookie_or_origin(app):
    web = client(app)
    enroll(web)
    token = web.post("/api/auth/tokens", json={"name": "pixel"}).json()["token"]

    device = TestClient(app, base_url=ORIGIN)  # no cookies, no Origin header
    auth = {"Authorization": f"Bearer {token}"}
    assert device.get("/api/sessions", headers=auth).status_code == 200
    created = device.post(
        "/api/sessions",
        headers=auth,
        json={
            "id": str(uuid.uuid4()),
            "device_id": "pixel",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "started_at_monotonic_ns": 0,
            "sample_rate": 16_000,
        },
    )
    assert created.status_code == 200  # bearer requests are exempt from the Origin check

    token_id = web.get("/api/auth/tokens").json()[0]["id"]
    assert web.delete(f"/api/auth/tokens/{token_id}").status_code == 200
    assert device.get("/api/sessions", headers=auth).status_code == 401


def test_logout_invalidates_the_session_cookie(app):
    c = client(app)
    enroll(c)
    assert c.get("/api/auth/session").status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/session").status_code == 401
    assert c.get("/api/sessions").status_code == 401


def test_remembered_browser_skips_the_code_on_the_next_sign_in(app):
    c = client(app)
    secret, _ = enroll(c)
    assert c.post("/api/auth/logout").status_code == 200

    assert sign_in(c, secret, remember=True)["remembered"] is True

    # same browser, fresh sign-in: the password is still asked, the code is not
    assert c.post("/api/auth/logout").status_code == 200
    back = login(c).json()
    assert back["mfa_required"] is False and back["remembered"] is True
    assert c.get("/api/sessions").status_code == 200


def test_a_browser_is_only_remembered_when_asked(app):
    c = client(app)
    secret, _ = enroll(c)
    assert c.post("/api/auth/logout").status_code == 200
    assert sign_in(c, secret)["remembered"] is False

    assert c.post("/api/auth/logout").status_code == 200
    assert login(c).json()["mfa_required"] is True
    assert c.get("/api/sessions").status_code == 403


def test_trust_cookie_waives_the_code_but_never_the_password(app):
    c = client(app)
    secret, _ = enroll(c)
    assert c.post("/api/auth/logout").status_code == 200
    sign_in(c, secret, remember=True)
    trust = c.cookies.get("__Host-nocturne_device")
    assert trust

    # a browser holding only the trust cookie is not signed in at all
    bare = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    bare.cookies.set("__Host-nocturne_device", trust)
    assert bare.get("/api/sessions").status_code == 401
    assert bare.get("/api/auth/session").status_code == 401
    assert login(bare, "wrong").status_code == 401
    assert bare.get("/api/sessions").status_code == 401


def test_forgetting_the_browser_brings_the_code_back(app):
    c = client(app)
    secret, _ = enroll(c)
    assert c.post("/api/auth/logout").status_code == 200
    sign_in(c, secret, remember=True)

    devices = c.get("/api/auth/devices").json()
    assert len(devices) == 1 and devices[0]["current"] is True
    assert c.delete(f"/api/auth/devices/{devices[0]['id']}").status_code == 200
    assert c.get("/api/auth/devices").json() == []

    assert c.post("/api/auth/logout").status_code == 200
    assert login(c).json()["mfa_required"] is True


def test_trust_expires_and_the_stale_row_is_dropped(app, tmp_path):
    c = client(app)
    secret, _ = enroll(c)
    assert c.post("/api/auth/logout").status_code == 200
    sign_in(c, secret, remember=True)

    with open_db(tmp_path) as db:
        row = db.scalars(select(TrustedDevice)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    assert c.post("/api/auth/logout").status_code == 200
    assert login(c).json()["mfa_required"] is True
    with open_db(tmp_path) as db:
        assert db.scalars(select(TrustedDevice)).all() == []


def test_changing_the_second_factor_forgets_every_browser(app, tmp_path):
    c = client(app)
    assert login(c).json()["needs_enrollment"] is True
    secret = c.post("/api/auth/mfa/setup").json()["secret"]
    enabled = c.post(
        "/api/auth/mfa/enable", json={"code": totp.now_code(secret), "remember": True}
    )
    assert enabled.status_code == 200 and enabled.json()["remembered"] is True
    with open_db(tmp_path) as db:
        assert len(db.scalars(select(TrustedDevice)).all()) == 1

    off = c.post(
        "/api/auth/mfa/disable",
        json={
            "password": PASSWORD,
            "code": totp.now_code(secret, at=time.time() + totp.PERIOD),
        },
    )
    assert off.status_code == 200
    with open_db(tmp_path) as db:
        assert db.scalars(select(TrustedDevice)).all() == []
    assert c.post("/api/auth/logout").status_code == 200
    assert login(c).json()["needs_enrollment"] is True


def test_dashboard_assets_revalidate_instead_of_going_stale(app):
    c = client(app)
    assert c.get("/api/health").headers["cache-control"] == "no-store"
    asset = c.get("/styles.css")
    assert asset.status_code == 200
    # unversioned filename: the browser must ask before reusing yesterday's deploy
    assert asset.headers["cache-control"] == "no-cache"
    assert asset.headers["etag"]
