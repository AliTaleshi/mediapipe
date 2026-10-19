"""API key and secret generation. Only SHA-256 hashes of API keys are stored."""

import hashlib
import hmac
import secrets

KEY_PREFIX = "mp"


def generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key, lookup_prefix, sha256_hash). The full key is shown once."""
    prefix = secrets.token_hex(4)
    full = f"{KEY_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
    return full, prefix, hash_api_key(full)


def hash_api_key(key: str) -> str:
    # Keys are 256-bit random values, so a fast hash is appropriate (no need for bcrypt).
    return hashlib.sha256(key.encode()).hexdigest()


def parse_api_key_prefix(key: str) -> str | None:
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1]


def verify_api_key(key: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(key), expected_hash)


def generate_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)
