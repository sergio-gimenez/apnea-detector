from apnea_api import totp


def test_now_code_round_trips_within_window():
    secret = totp.generate_secret()
    at = 1_800_000_000.0
    code = totp.now_code(secret, at=at)
    assert code.isdigit() and len(code) == totp.DIGITS
    assert totp.verify(secret, code, at=at)
    assert totp.verify(secret, code, at=at + totp.PERIOD)  # one step of skew tolerated


def test_verify_rejects_stale_and_malformed_codes():
    secret = totp.generate_secret()
    at = 1_800_000_000.0
    code = totp.now_code(secret, at=at)
    assert not totp.verify(secret, code, at=at + 5 * totp.PERIOD)
    assert not totp.verify(secret, "12345", at=at)
    assert not totp.verify(secret, "abcdef", at=at)
    assert not totp.verify(secret, "", at=at)


def test_match_step_returns_the_consuming_step():
    secret = totp.generate_secret()
    at = 1_800_000_000.0
    base = int(at // totp.PERIOD)
    assert totp.match_step(secret, totp.now_code(secret, at=at), at=at) == base
    # a code from the previous window still resolves, to its own (earlier) step
    prev = totp.now_code(secret, at=at - totp.PERIOD)
    assert totp.match_step(secret, prev, at=at) == base - 1
    # far out of the window: no match
    assert totp.match_step(secret, totp.now_code(secret, at=at), at=at + 10 * totp.PERIOD) is None


def test_provisioning_uri_is_otpauth():
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, "sergio", issuer="Nocturne")
    assert uri.startswith("otpauth://totp/Nocturne:sergio?")
    assert f"secret={secret}" in uri
    assert "issuer=Nocturne" in uri
