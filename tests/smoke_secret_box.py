#!/usr/bin/env python3
"""Guard for secret_box.py field encryption (no DB, no server, no real secrets).

Uses keys generated in-process and synthetic plaintext only.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import secret_box  # noqa: E402

FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc as e:
        return e
    except Exception:
        return None
    return None


PLAIN = "synthetic-refresh-token-9X"
K1, K2 = secret_box.generate_key(), secret_box.generate_key()

# --- fail closed with no key ---
os.environ.pop(secret_box.KEY_ENV, None)
ok("not configured without a key", secret_box.is_configured() is False)
ok("encrypt fails closed without key", raises(secret_box.SecretBoxUnavailable, secret_box.encrypt_text, PLAIN) is not None)
ok("decrypt fails closed without key", raises(secret_box.SecretBoxUnavailable, secret_box.decrypt_text, "v1:x") is not None)
os.environ[secret_box.KEY_ENV] = "not-a-valid-key"
e = raises(secret_box.SecretBoxUnavailable, secret_box.encrypt_text, PLAIN)
ok("invalid key fails closed", e is not None)
ok("invalid-key error does not echo the key", e is not None and "not-a-valid-key" not in str(e))

# --- roundtrip ---
os.environ[secret_box.KEY_ENV] = K1
tok = secret_box.encrypt_text(PLAIN)
ok("configured with a valid key", secret_box.is_configured())
ok("ciphertext has version prefix", tok.startswith("v1:"))
ok("ciphertext does not contain plaintext", PLAIN not in tok)
ok("roundtrip", secret_box.decrypt_text(tok) == PLAIN)
ok("unicode roundtrip", secret_box.decrypt_text(secret_box.encrypt_text("ñandú — 9X")) == "ñandú — 9X")
ok("encryption is randomized", secret_box.encrypt_text(PLAIN) != tok)

# --- tamper / wrong key / bad format ---
flipped = tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB")
e = raises(secret_box.SecretBoxError, secret_box.decrypt_text, flipped)
ok("tampered ciphertext rejected", e is not None)
ok("error text carries no plaintext or ciphertext", e is not None and PLAIN not in str(e) and tok not in str(e))
os.environ[secret_box.KEY_ENV] = K2
ok("wrong key rejected", raises(secret_box.SecretBoxError, secret_box.decrypt_text, tok) is not None)
ok("unprefixed token rejected", raises(secret_box.SecretBoxError, secret_box.decrypt_text, tok[3:]) is not None)
ok("non-str rejected", raises(secret_box.SecretBoxError, secret_box.decrypt_text, None) is not None)
ok("encrypt rejects bytes", raises(TypeError, secret_box.encrypt_text, b"x") is not None)

# --- rotation: new key first, old key still decrypts, rewrap moves to the new key ---
os.environ[secret_box.KEY_ENV] = f"{K2},{K1}"
ok("old-key ciphertext still decrypts after rotation", secret_box.decrypt_text(tok) == PLAIN)
new_tok = secret_box.rewrap(tok)
ok("rewrap changes the ciphertext", new_tok != tok)
os.environ[secret_box.KEY_ENV] = K2   # old key retired
ok("rewrapped token decrypts with only the new key", secret_box.decrypt_text(new_tok) == PLAIN)
ok("un-rewrapped token fails once old key is retired", raises(secret_box.SecretBoxError, secret_box.decrypt_text, tok) is not None)
ok("rewrap rejects garbage", raises(secret_box.SecretBoxError, secret_box.rewrap, "v1:garbage") is not None)

# --- repo hygiene: key is referenced via 1Password, never a literal; module never logs ---
tmpl = (ROOT / ".env.template").read_text(encoding="utf-8")
m = re.search(r'^SSC_FIELD_ENCRYPTION_KEY="(.*)"\s*$', tmpl, re.M)
ok(".env.template references the key via op://", bool(m) and m.group(1).startswith("op://"))
src = (ROOT / "secret_box.py").read_text(encoding="utf-8")
ok("secret_box has no logging or print of values", "logging" not in src and "print(plaintext" not in src)

if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: secret_box field encryption")
