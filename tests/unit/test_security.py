from app.core.security import (
    generate_api_key,
    generate_webhook_secret,
    hash_api_key,
    parse_api_key_prefix,
    verify_api_key,
)


def test_generated_key_roundtrip() -> None:
    full, prefix, key_hash = generate_api_key()
    assert full.startswith(f"mp_{prefix}_")
    assert parse_api_key_prefix(full) == prefix
    assert key_hash == hash_api_key(full)
    assert full not in key_hash
    assert verify_api_key(full, key_hash)
    assert not verify_api_key(full + "x", key_hash)


def test_keys_are_unique() -> None:
    assert generate_api_key()[0] != generate_api_key()[0]


def test_parse_rejects_malformed() -> None:
    for bad in ["", "abc", "mp_", "mp__x", "xx_abcd_secret", "mp_abcd"]:
        assert parse_api_key_prefix(bad) is None


def test_webhook_secret_format() -> None:
    assert generate_webhook_secret().startswith("whsec_")
