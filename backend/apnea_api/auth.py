"""Password login, cookie sessions, device tokens, and TOTP MFA.

The service is a single-operator research prototype reachable over a public
tunnel, so "proper auth" here means: one account with a scrypt-hashed password, a
mandatory authenticator-app second factor, opaque server-side sessions in an
``HttpOnly`` cookie, and separately revocable bearer tokens for the phone. No
roles, no signup.

The rate limiter keeps its state in this process. That is sufficient because the
service runs as a single uvicorn worker (see ``deploy/apnea-detector.service``);
adding ``--workers`` would need a shared store instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import segno
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import totp
from .models import ApiToken, AuthSession, MfaRecoveryCode, User, utc_now

SESSION_TTL = timedelta(days=14)
SESSION_ABSOLUTE_TTL = timedelta(days=30)
PARTIAL_SESSION_TTL = timedelta(minutes=10)
RECOVERY_CODE_COUNT = 10
_TOUCH_INTERVAL = timedelta(seconds=60)

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2


# ---------------------------------------------------------------- passwords ----
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        ]
    )


# A fixed hash to verify against when the username is unknown, so a missing
# account costs the same scrypt time as a wrong password (no enumeration oracle).
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=128 * int(n) * int(r) * 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


# ------------------------------------------------------- opaque credentials ----
def new_credential() -> tuple[str, str, str]:
    """``(id, secret, "id.secret")``. The id addresses the row; only the secret's
    hash is stored, so the plaintext exists exactly once, in the client's hands."""
    cred_id = secrets.token_hex(16)
    secret = secrets.token_urlsafe(32)
    return cred_id, secret, f"{cred_id}.{secret}"


def hash_secret(secret: str) -> str:
    # secrets here carry ~256 bits of entropy, so a plain SHA-256 (no salt, no
    # stretching) is enough and keeps lookups cheap.
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _split_credential(value: str) -> tuple[str, str] | None:
    cred_id, _, secret = value.partition(".")
    if not cred_id or not secret:
        return None
    return cred_id, secret


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    # 64 bits per code; brute force is hopeless even before the rate limiter.
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(count)]


def _normalize_recovery_code(code: str) -> str:
    return code.strip().lower().replace(" ", "").replace("-", "")


# -------------------------------------------------------------- rate limiter ----
class RateLimiter:
    """In-process failure counter with exponential lockout.

    Each concern (password, MFA code) checks two keys: ``username`` and
    ``username|ip``. The per-IP key stops a single source fast; the bare-username
    key is the ceiling that a botnet rotating IPs (or forging ``X-Forwarded-For``)
    still cannot get past. The dict is pruned and capped so those forged keys
    cannot grow it without bound.
    """

    _MAX_KEYS = 8192

    def __init__(self, threshold: int = 8, base_lock: timedelta = timedelta(minutes=15)):
        self._threshold = threshold
        self._base_lock = base_lock
        self._state: dict[str, tuple[int, float]] = {}

    def check(self, *keys: str) -> None:
        now = time.monotonic()
        locked = max((self._state.get(key, (0, 0.0))[1] for key in keys), default=0.0)
        if locked > now:
            raise HTTPException(429, f"Too many attempts; retry in {round(locked - now)}s")

    def record_failure(self, *keys: str) -> None:
        self._prune()
        for key in keys:
            fails = self._state.get(key, (0, 0.0))[0] + 1
            locked_until = 0.0
            if fails >= self._threshold:
                lock = self._base_lock.total_seconds() * (2 ** (fails - self._threshold))
                locked_until = time.monotonic() + min(lock, 86_400)
            self._state[key] = (fails, locked_until)

    def reset(self, *keys: str) -> None:
        for key in keys:
            self._state.pop(key, None)

    def _prune(self) -> None:
        if len(self._state) < self._MAX_KEYS:
            return
        now = time.monotonic()
        for key in [k for k, (_, until) in self._state.items() if until <= now]:
            del self._state[key]
        if len(self._state) >= self._MAX_KEYS:  # still full: drop soonest-to-expire
            for key in sorted(self._state, key=lambda k: self._state[k][1])[: self._MAX_KEYS // 2]:
                del self._state[key]


# --------------------------------------------------------------- CSRF / QR ----
def check_origin(request: Request, trusted_origins: set[str]) -> bool:
    """Guard state-changing cookie requests against cross-site submission.

    TLS is terminated by the tunnel/proxy, so the origin the server sees is
    ``http`` while the browser's ``Origin`` is ``https``. Matching on host is the
    reliable comparison here; a full ``scheme://host`` match and an explicit
    ``APNEA_TRUSTED_ORIGINS`` entry are also accepted.
    """
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if not referer:
            return False
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}"
    if origin == "null":
        return False
    if origin in trusted_origins:
        return True
    host = request.headers.get("host", "")
    if not host:
        return False
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    if origin == f"{scheme}://{host}":
        return True
    # scheme can still be wrong behind a proxy that does not set the header, so a
    # bare host match is the backstop; the origin port, if any, must still agree.
    return urlsplit(origin).netloc.lower() == host.lower()


def render_qr_data_uri(uri: str) -> str:
    return segno.make(uri, error="m").svg_data_uri(scale=4, border=2, dark="#0b1014", light="#ffffff")


# ---------------------------------------------------------------- principal ----
@dataclass
class Principal:
    via: str  # "session" | "token" | "insecure-dev"
    user: User | None = None
    session: AuthSession | None = None

    @property
    def fully_authorized(self) -> bool:
        if self.via in {"token", "insecure-dev"}:
            return True
        return bool(
            self.session
            and self.session.mfa_satisfied
            and self.user is not None
            and self.user.mfa_enabled
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def session_cookie_name(secure: bool) -> str:
    # __Host- pins the cookie to this exact origin and forbids a Domain attribute;
    # it is only valid alongside Secure, so it is dropped for plain-http dev.
    return "__Host-nocturne_session" if secure else "nocturne_session"


def _load_session(db: Session, cookie_value: str) -> AuthSession | None:
    parts = _split_credential(cookie_value)
    if not parts:
        return None
    row = db.get(AuthSession, parts[0])
    if row is None or not hmac.compare_digest(row.secret_hash, hash_secret(parts[1])):
        return None
    if _aware(row.expires_at) <= utc_now():
        db.delete(row)
        db.commit()
        return None
    return row


def _load_api_token(db: Session, raw: str) -> ApiToken | None:
    parts = _split_credential(raw)
    if not parts:
        return None
    row = db.get(ApiToken, parts[0])
    if row is None or row.revoked_at is not None:
        return None
    if not hmac.compare_digest(row.token_hash, hash_secret(parts[1])):
        return None
    if row.expires_at is not None and _aware(row.expires_at) <= utc_now():
        return None
    return row


def resolve_principal(request: Request, db: Session, cookie_name: str) -> Principal | None:
    """The authenticated party behind a request, or ``None``.

    A presented-but-invalid bearer token is a hard failure rather than a fall
    through to the cookie, so a stale device token cannot silently borrow a
    browser session.
    """
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = _load_api_token(db, authorization[7:].strip())
        if token is None:
            return None
        now = utc_now()
        if token.last_used_at is None or now - _aware(token.last_used_at) > _TOUCH_INTERVAL:
            token.last_used_at = now
            db.commit()
        return Principal(via="token", user=token.user)

    cookie = request.cookies.get(cookie_name)
    if not cookie:
        return None
    session = _load_session(db, cookie)
    if session is None:
        return None
    now = utc_now()
    if now - _aware(session.last_seen_at) > _TOUCH_INTERVAL:
        session.last_seen_at = now
        if session.mfa_satisfied:
            ceiling = _aware(session.created_at) + SESSION_ABSOLUTE_TTL
            session.expires_at = min(now + SESSION_TTL, ceiling)
        db.commit()
    return Principal(via="session", user=session.user, session=session)


# ------------------------------------------------------------- request bodies ----
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=40)


class TokenCreateBody(BaseModel):
    name: str = Field(default="device", min_length=1, max_length=120)
    days: int | None = Field(default=None, ge=1, le=3650)


class DisableMfaBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=1, max_length=40)


# ------------------------------------------------------------------- router ----
def build_auth_router(
    database,
    *,
    secure_cookies: bool,
    trusted_origins: set[str],
    insecure_dev: bool = False,
    trust_forwarded_for: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])
    cookie_name = session_cookie_name(secure_cookies)
    # per-IP lockout is tight; the bare-username ceiling only trips under a
    # distributed attack, so it is loose enough that normal fat-fingering misses it.
    login_limiter = RateLimiter(threshold=8)
    login_ceiling = RateLimiter(threshold=64, base_lock=timedelta(minutes=15))
    mfa_limiter = RateLimiter(threshold=8)
    mfa_ceiling = RateLimiter(threshold=64, base_lock=timedelta(minutes=15))

    def get_db():
        with database() as db:
            yield db

    def client_ip(request: Request) -> str:
        # X-Forwarded-For is forgeable by anything talking straight to the origin,
        # so it is trusted only when the operator confirms a proxy always sets it.
        if trust_forwarded_for:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "?"

    def guard_keys(request: Request, username: str) -> tuple[str, str]:
        name = username.lower()
        return name, f"{name}|{client_ip(request)}"

    def write_cookie(response: Response, value: str, max_age: int) -> None:
        response.set_cookie(
            cookie_name,
            value,
            max_age=max_age,
            httponly=True,
            secure=secure_cookies,
            samesite="lax",
            path="/",
        )

    def issue_session(
        db: Session, user: User, request: Request, *, mfa_satisfied: bool
    ) -> tuple[AuthSession, str]:
        """Create a fresh session row; return it and its cookie value. Used at
        login and again when MFA is satisfied, so the identifier always changes on
        a privilege change and a pre-MFA token dies the moment the factor clears."""
        cred_id, secret, value = new_credential()
        now = utc_now()
        row = AuthSession(
            id=cred_id,
            secret_hash=hash_secret(secret),
            mfa_satisfied=mfa_satisfied,
            created_at=now,
            last_seen_at=now,
            expires_at=now + (SESSION_TTL if mfa_satisfied else PARTIAL_SESSION_TTL),
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
            client_ip=client_ip(request),
        )
        row.user = user
        db.add(row)
        return row, value

    def current_session(request: Request, db: Session) -> AuthSession:
        cookie = request.cookies.get(cookie_name)
        session = _load_session(db, cookie) if cookie else None
        if session is None:
            raise HTTPException(401, "Not signed in")
        return session

    def require_enrolling(session: AuthSession) -> User:
        """A session allowed to run MFA setup: already MFA-verified, or a user who
        has no MFA configured yet."""
        if session.user.mfa_enabled and not session.mfa_satisfied:
            raise HTTPException(403, "Finish the second factor first")
        return session.user

    def require_full(session: AuthSession) -> User:
        if not (session.mfa_satisfied and session.user.mfa_enabled):
            raise HTTPException(403, {"detail": "MFA required", "needs_mfa": True})
        return session.user

    def session_state(session: AuthSession) -> dict:
        user = session.user
        return {
            "username": user.username,
            "mfa_enabled": user.mfa_enabled,
            "mfa_required": user.mfa_enabled and not session.mfa_satisfied,
            "needs_enrollment": not user.mfa_enabled,
        }

    @router.get("/session")
    def read_session(request: Request, db: Session = Depends(get_db)) -> dict:
        cookie = request.cookies.get(cookie_name)
        session = _load_session(db, cookie) if cookie else None
        if session is None:
            if insecure_dev:
                # auth is disabled for this run; let the dashboard straight through
                return {
                    "username": "dev",
                    "mfa_enabled": True,
                    "mfa_required": False,
                    "needs_enrollment": False,
                }
            raise HTTPException(401, "Not signed in")
        return session_state(session)

    @router.post("/login")
    def login(
        body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)
    ) -> dict:
        name_key, ip_key = guard_keys(request, body.username)
        login_limiter.check(ip_key)
        login_ceiling.check(name_key)
        user = db.scalar(select(User).where(User.username == body.username))
        # verify against a dummy hash when the user is unknown: same cost, no oracle
        if not verify_password(body.password, user.password_hash if user else _DUMMY_PASSWORD_HASH):
            login_limiter.record_failure(ip_key)
            login_ceiling.record_failure(name_key)
            raise HTTPException(401, "Wrong username or password")
        login_limiter.reset(ip_key)
        login_ceiling.reset(name_key)

        satisfied = not user.mfa_enabled
        session, value = issue_session(db, user, request, mfa_satisfied=satisfied)
        db.commit()
        write_cookie(response, value, int(SESSION_TTL.total_seconds()))
        return session_state(session)

    @router.post("/logout")
    def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
        cookie = request.cookies.get(cookie_name)
        session = _load_session(db, cookie) if cookie else None
        if session is not None:
            db.delete(session)
            db.commit()
        response.delete_cookie(
            cookie_name, path="/", httponly=True, secure=secure_cookies, samesite="lax"
        )
        return {"ok": True}

    @router.post("/mfa/verify")
    def verify_mfa(
        body: CodeBody, request: Request, response: Response, db: Session = Depends(get_db)
    ) -> dict:
        session = current_session(request, db)
        user = session.user
        if session.mfa_satisfied:
            return session_state(session)
        if not user.mfa_enabled or not user.totp_secret:
            raise HTTPException(400, "MFA is not set up for this account")
        name_key, ip_key = guard_keys(request, user.username)
        mfa_limiter.check(ip_key)
        mfa_ceiling.check(name_key)

        code = body.code.strip()
        accepted = False
        step = totp.match_step(user.totp_secret, code)
        if step is not None:
            if user.totp_last_step is not None and step <= user.totp_last_step:
                mfa_limiter.record_failure(ip_key)
                mfa_ceiling.record_failure(name_key)
                raise HTTPException(401, "That code was already used; wait for the next one")
            user.totp_last_step = step
            accepted = True
        else:
            wanted = _normalize_recovery_code(code)
            for candidate in user.recovery_codes:
                if candidate.used_at is None and hmac.compare_digest(
                    candidate.code_hash, hash_secret(wanted)
                ):
                    candidate.used_at = utc_now()
                    accepted = True
                    break
        if not accepted:
            mfa_limiter.record_failure(ip_key)
            mfa_ceiling.record_failure(name_key)
            raise HTTPException(401, "Invalid code")

        mfa_limiter.reset(ip_key)
        mfa_ceiling.reset(name_key)
        db.delete(session)
        fresh, value = issue_session(db, user, request, mfa_satisfied=True)
        db.commit()
        write_cookie(response, value, int(SESSION_TTL.total_seconds()))
        return session_state(fresh)

    @router.post("/mfa/setup")
    def setup_mfa(request: Request, db: Session = Depends(get_db)) -> dict:
        session = current_session(request, db)
        user = require_enrolling(session)
        secret = totp.generate_secret()
        user.pending_totp_secret = secret
        db.commit()
        uri = totp.provisioning_uri(secret, user.username)
        return {"secret": secret, "otpauth_uri": uri, "qr_data_uri": render_qr_data_uri(uri)}

    @router.post("/mfa/enable")
    def enable_mfa(
        body: CodeBody, request: Request, response: Response, db: Session = Depends(get_db)
    ) -> dict:
        session = current_session(request, db)
        user = require_enrolling(session)
        if not user.pending_totp_secret:
            raise HTTPException(400, "Call /mfa/setup first")
        if not totp.verify(user.pending_totp_secret, body.code.strip()):
            raise HTTPException(401, "Code did not match; check the authenticator and retry")

        user.totp_secret = user.pending_totp_secret
        user.pending_totp_secret = None
        user.mfa_enabled = True
        db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
        codes = generate_recovery_codes()
        for code in codes:
            db.add(
                MfaRecoveryCode(
                    user_id=user.id, code_hash=hash_secret(_normalize_recovery_code(code))
                )
            )
        db.delete(session)
        fresh, value = issue_session(db, user, request, mfa_satisfied=True)
        db.commit()
        write_cookie(response, value, int(SESSION_TTL.total_seconds()))
        return {"recovery_codes": codes, **session_state(fresh)}

    @router.post("/mfa/disable")
    def disable_mfa(
        body: DisableMfaBody, request: Request, db: Session = Depends(get_db)
    ) -> dict:
        session = current_session(request, db)
        user = require_full(session)
        if not verify_password(body.password, user.password_hash) or not totp.verify(
            user.totp_secret or "", body.code.strip()
        ):
            raise HTTPException(401, "Password or code incorrect")
        user.mfa_enabled = False
        user.totp_secret = None
        user.pending_totp_secret = None
        db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
        db.commit()
        return {"ok": True}

    @router.get("/tokens")
    def list_tokens(request: Request, db: Session = Depends(get_db)) -> list[dict]:
        user = require_full(current_session(request, db))
        rows = db.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
            .order_by(ApiToken.created_at.desc())
        )
        return [
            {
                "id": row.id,
                "name": row.name,
                "created_at": _aware(row.created_at).isoformat(),
                "last_used_at": _aware(row.last_used_at).isoformat() if row.last_used_at else None,
                "expires_at": _aware(row.expires_at).isoformat() if row.expires_at else None,
            }
            for row in rows
        ]

    @router.post("/tokens")
    def create_token(
        body: TokenCreateBody, request: Request, db: Session = Depends(get_db)
    ) -> dict:
        user = require_full(current_session(request, db))
        cred_id, secret, value = new_credential()
        row = ApiToken(
            id=cred_id,
            user_id=user.id,
            name=body.name,
            token_hash=hash_secret(secret),
            expires_at=utc_now() + timedelta(days=body.days) if body.days else None,
        )
        db.add(row)
        db.commit()
        return {"id": cred_id, "name": row.name, "token": value}

    @router.delete("/tokens/{token_id}")
    def revoke_token(token_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
        user = require_full(current_session(request, db))
        row = db.get(ApiToken, token_id)
        if row is None or row.user_id != user.id or row.revoked_at is not None:
            raise HTTPException(404, "Token not found")
        row.revoked_at = utc_now()
        db.commit()
        return {"ok": True}

    @router.get("/sessions")
    def list_sessions(request: Request, db: Session = Depends(get_db)) -> list[dict]:
        session = current_session(request, db)
        user = require_full(session)
        rows = db.scalars(
            select(AuthSession)
            .where(AuthSession.user_id == user.id)
            .order_by(AuthSession.last_seen_at.desc())
        )
        return [
            {
                "id": row.id,
                "current": row.id == session.id,
                "created_at": _aware(row.created_at).isoformat(),
                "last_seen_at": _aware(row.last_seen_at).isoformat(),
                "user_agent": row.user_agent,
                "client_ip": row.client_ip,
            }
            for row in rows
        ]

    @router.delete("/sessions/{session_id}")
    def revoke_session(session_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
        user = require_full(current_session(request, db))
        row = db.get(AuthSession, session_id)
        if row is None or row.user_id != user.id:
            raise HTTPException(404, "Session not found")
        db.delete(row)
        db.commit()
        return {"ok": True}

    return router
