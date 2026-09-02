"""``apnea-admin`` — provision the operator account from a shell.

There is no signup route; the first user (and password resets, MFA resets, and
headless device tokens for the recorder) are created here, next to the running
service. Mirrors ``apnea-garmin-login`` in ``garmin.py``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from .auth import hash_password, hash_secret, new_credential
from .models import ApiToken, Base, MfaRecoveryCode, User, utc_now

MIN_PASSWORD_LENGTH = 10


def _open_sessionmaker():
    root = Path(os.getenv("APNEA_DATA_DIR", "./data")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    database_url = os.getenv("DATABASE_URL", f"sqlite:///{root / 'apnea.db'}")
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def create_user(db: Session, username: str, password: str) -> User:
    username = username.strip()
    if not username:
        raise ValueError("Username must not be empty")
    if db.scalar(select(User).where(User.username == username)):
        raise ValueError(f"User {username!r} already exists")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password.lower() == username.lower():
        raise ValueError("Password must not equal the username")
    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


def mint_token(db: Session, user: User, name: str, days: int | None) -> str:
    cred_id, secret, value = new_credential()
    db.add(
        ApiToken(
            id=cred_id,
            user_id=user.id,
            name=name,
            token_hash=hash_secret(secret),
            expires_at=utc_now() + timedelta(days=days) if days else None,
        )
    )
    db.commit()
    return value


def _require_user(db: Session, username: str) -> User:
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user is None:
        raise SystemExit(f"No such user: {username}")
    return user


def _prompt_new_password() -> str:
    first = getpass.getpass("New password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("Passwords did not match")
    return first


def _cmd_create_user(db: Session, args: argparse.Namespace) -> None:
    password = _prompt_new_password()
    try:
        create_user(db, args.username, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Created {args.username}. Sign in on the dashboard and scan the MFA QR to finish.")


def _cmd_list_users(db: Session, _args: argparse.Namespace) -> None:
    rows = list(db.scalars(select(User).order_by(User.created_at)))
    if not rows:
        # nothing on stdout, so scripts can test for an empty account list
        print("No users yet — run: apnea-admin create-user <username>", file=sys.stderr)
        return
    for user in rows:
        active = sum(1 for t in user.api_tokens if t.revoked_at is None)
        mfa = "mfa:on" if user.mfa_enabled else "mfa:OFF"
        print(f"{user.username:<24} {mfa:<8} tokens:{active:<3} since {user.created_at:%Y-%m-%d}")


def _cmd_passwd(db: Session, args: argparse.Namespace) -> None:
    user = _require_user(db, args.username)
    password = _prompt_new_password()
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    user.password_hash = hash_password(password)
    user.password_changed_at = utc_now()
    db.commit()
    print(f"Password updated for {user.username}.")


def _cmd_reset_mfa(db: Session, args: argparse.Namespace) -> None:
    user = _require_user(db, args.username)
    user.mfa_enabled = False
    user.totp_secret = None
    user.pending_totp_secret = None
    db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
    db.commit()
    print(f"MFA cleared for {user.username}. Next sign-in will prompt for re-enrolment.")


def _cmd_mint_token(db: Session, args: argparse.Namespace) -> None:
    user = _require_user(db, args.username)
    value = mint_token(db, user, args.name, args.days)
    horizon = f"expires in {args.days}d" if args.days else "no expiry"
    print(f"Device token for {user.username} ({args.name}, {horizon}) — store it now:\n\n{value}\n")


def _cmd_list_tokens(db: Session, args: argparse.Namespace) -> None:
    user = _require_user(db, args.username)
    if not user.api_tokens:
        print("(no tokens)")
        return
    for token in sorted(user.api_tokens, key=lambda t: t.created_at):
        state = "revoked" if token.revoked_at else "active"
        used = f"{token.last_used_at:%Y-%m-%d}" if token.last_used_at else "never used"
        print(f"{token.id}  {token.name:<16} {state:<8} {used}")


def _cmd_revoke_token(db: Session, args: argparse.Namespace) -> None:
    token = db.get(ApiToken, args.token_id)
    if token is None:
        raise SystemExit(f"No such token: {args.token_id}")
    if token.revoked_at is None:
        token.revoked_at = utc_now()
        db.commit()
    print(f"Revoked token {token.id} ({token.name}).")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="apnea-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-users").set_defaults(func=_cmd_list_users)
    for name in ("create-user", "passwd", "reset-mfa", "list-tokens"):
        entry = sub.add_parser(name)
        entry.add_argument("username")
        entry.set_defaults(
            func={
                "create-user": _cmd_create_user,
                "passwd": _cmd_passwd,
                "reset-mfa": _cmd_reset_mfa,
                "list-tokens": _cmd_list_tokens,
            }[name]
        )

    mint = sub.add_parser("mint-token")
    mint.add_argument("username")
    mint.add_argument("--name", default="device")
    mint.add_argument("--days", type=int, default=None)
    mint.set_defaults(func=_cmd_mint_token)

    revoke = sub.add_parser("revoke-token")
    revoke.add_argument("token_id")
    revoke.set_defaults(func=_cmd_revoke_token)

    args = parser.parse_args(argv)
    database = _open_sessionmaker()
    with database() as db:
        args.func(db, args)


if __name__ == "__main__":
    sys.exit(main())
