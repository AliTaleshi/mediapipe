import hashlib
import hmac

import pytest

from app.core.errors import UnprocessableError
from app.services.webhooks import (
    compute_signature,
    signature_header,
    validate_webhook_url,
    verify_signature,
)

BODY = b'{"event":"file.processed"}'
SECRET = "whsec_test"


def test_signature_is_hmac_sha256_over_timestamp_dot_body() -> None:
    expected = hmac.new(SECRET.encode(), b"1700000000." + BODY, hashlib.sha256).hexdigest()
    assert compute_signature(SECRET, 1700000000, BODY) == expected
    assert signature_header(SECRET, BODY, 1700000000) == f"t=1700000000,v1={expected}"


def test_verify_accepts_valid_signature() -> None:
    header = signature_header(SECRET, BODY, 1000)
    assert verify_signature(SECRET, header, BODY, now=1100)


@pytest.mark.parametrize(
    ("secret", "body", "now"),
    [
        ("whsec_other", BODY, 1000),  # wrong secret
        (SECRET, BODY + b" ", 1000),  # tampered body
        (SECRET, BODY, 1000 + 301),  # replayed after the tolerance window
        (SECRET, BODY, 1000 - 301),  # timestamp from the future
    ],
)
def test_verify_rejects_bad_signatures(secret: str, body: bytes, now: float) -> None:
    header = signature_header(SECRET, BODY, 1000)
    assert not verify_signature(secret, header, body, now=now)


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1=00", "t=1000", "t=1000,v1="])
def test_verify_rejects_malformed_headers(header: str) -> None:
    assert not verify_signature(SECRET, header, BODY, now=1000)


def test_timestamp_cannot_be_swapped() -> None:
    sig = compute_signature(SECRET, 1000, BODY)
    assert not verify_signature(SECRET, f"t=1001,v1={sig}", BODY, now=1001)


@pytest.mark.parametrize(
    "url",
    ["https://example.com/hook", "http://8.8.8.8/x", "https://hooks.example.com:8443/a?b=c"],
)
def test_valid_urls(url: str) -> None:
    validate_webhook_url(url, allow_private=False)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "javascript:alert(1)",
        "not a url",
        "https://user:pw@example.com/",
        "http://localhost/hook",
        "http://app.localhost/hook",
        "http://127.0.0.1:8000/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
    ],
)
def test_unsafe_urls_rejected(url: str) -> None:
    with pytest.raises(UnprocessableError) as exc:
        validate_webhook_url(url, allow_private=False)
    assert exc.value.code == "invalid_webhook_url"


def test_private_urls_allowed_when_configured() -> None:
    validate_webhook_url("http://127.0.0.1:8000/", allow_private=True)
