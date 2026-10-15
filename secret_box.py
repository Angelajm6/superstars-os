"""Field-level encryption for secrets and inbox content stored in the database.

For OAuth refresh tokens, mail bodies, attachments metadata and similar values that must
not sit in plaintext in the DB (per CLAUDE.md secrets rule).

  * Algorithm : Fernet (AES-128-CBC + HMAC-SHA256, authenticated) from `cryptography`.
                Tampered or truncated ciphertext is rejected, never partially decrypted.
  * Key       : env SSC_FIELD_ENCRYPTION_KEY, injected at runtime from the 1Password
                "Dashboard Secrets" vault via `op run` (see .env.template). Never in code,
                never in .env, never logged.
  * Rotation  : the variable may hold several comma-separated keys. The FIRST encrypts;
                all of them decrypt. `rewrap()` re-encrypts a token under the first key.
  * Fail closed: no key, or an invalid key => SecretBoxUnavailable. There is NO plaintext
                fallback anywhere.
  * Logging   : errors carry a fixed message only — never plaintext, ciphertext or keys.

Tokens are stored as text: "v1:<fernet token>". The prefix leaves room to change scheme.
Generate a key (once, then store it in 1Password):  python secret_box.py --generate-key
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

KEY_ENV = "SSC_FIELD_ENCRYPTION_KEY"
_PREFIX = "v1:"


class SecretBoxUnavailable(RuntimeError):
    """No usable encryption key is configured. Callers must fail the operation."""


class SecretBoxError(ValueError):
    """Ciphertext is malformed, tampered with, or not decryptable with the configured keys."""


_cache: tuple = (None, None)   # (raw env value, MultiFernet)


def _box() -> MultiFernet:
    global _cache
    raw = os.environ.get(KEY_ENV, "")
    if not raw.strip():
        raise SecretBoxUnavailable(f"{KEY_ENV} is not set")
    if _cache[0] == raw:
        return _cache[1]
    try:
        fernets = [Fernet(k.strip().encode()) for k in raw.split(",") if k.strip()]
    except Exception:  # noqa: BLE001 — never echo the key material
        raise SecretBoxUnavailable(f"{KEY_ENV} is not a valid Fernet key list") from None
    if not fernets:
        raise SecretBoxUnavailable(f"{KEY_ENV} is not set")
    box = MultiFernet(fernets)
    _cache = (raw, box)
    return box


def is_configured() -> bool:
    try:
        _box()
        return True
    except SecretBoxUnavailable:
        return False


def generate_key() -> str:
    """A fresh random key. Store it in 1Password; never on disk."""
    return Fernet.generate_key().decode()


def encrypt_text(plaintext: str) -> str:
    if not isinstance(plaintext, str):
        raise TypeError("encrypt_text expects str")
    return _PREFIX + _box().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_text(token: str) -> str:
    if not isinstance(token, str) or not token.startswith(_PREFIX):
        raise SecretBoxError("unrecognized ciphertext format")
    try:
        return _box().decrypt(token[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise SecretBoxError("decryption failed") from None


_BPREFIX = b"v1b:"


def encrypt_bytes(data: bytes) -> bytes:
    """Whole files (Claude attachments) — same keys as encrypt_text, binary-safe."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("encrypt_bytes expects bytes")
    return _BPREFIX + _box().encrypt(bytes(data))


def decrypt_bytes(blob: bytes) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(_BPREFIX):
        raise SecretBoxError("unrecognized ciphertext format")
    try:
        return _box().decrypt(bytes(blob)[len(_BPREFIX):])
    except InvalidToken:
        raise SecretBoxError("decryption failed") from None


def rewrap(token: str) -> str:
    """Re-encrypt under the current (first) key — use after rotating keys."""
    if not isinstance(token, str) or not token.startswith(_PREFIX):
        raise SecretBoxError("unrecognized ciphertext format")
    try:
        return _PREFIX + _box().rotate(token[len(_PREFIX):].encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeError):
        raise SecretBoxError("decryption failed") from None


if __name__ == "__main__":
    import sys
    if "--generate-key" in sys.argv:
        print(generate_key())
    else:
        print("usage: python secret_box.py --generate-key")
