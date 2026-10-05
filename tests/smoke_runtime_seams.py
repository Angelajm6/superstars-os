#!/usr/bin/env python3
"""Guard for runtime_seams.py — test-only seams are inert unless a test run opts in,
and can never be enabled on Render / production. Synthetic values only, no DB/server.
"""
import base64
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runtime_seams as rs  # noqa: E402

FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def clean_env():
    for k in list(rs.SEAM_VARS) + [rs.ALLOW_ENV, "RENDER", "SSC_ENVIRONMENT"]:
        os.environ.pop(k, None)


# --- gating matrix ---
clean_env()
os.environ["DOC_SCAN_FAKE"] = "/tmp/synthetic.json"
ok("seam set but no opt-in => inert", rs.seam("DOC_SCAN_FAKE") == "")
ok("allowed() false without opt-in", rs.allowed() is False)
os.environ[rs.ALLOW_ENV] = "1"
ok("opt-in => seam value returned", rs.seam("DOC_SCAN_FAKE") == "/tmp/synthetic.json")
ok("unset seam returns empty", rs.seam("SSC_TRACE_FAKE") == "")
os.environ[rs.ALLOW_ENV] = "true"
ok("opt-in must be exactly '1'", rs.seam("DOC_SCAN_FAKE") == "")
os.environ[rs.ALLOW_ENV] = "1"
os.environ["RENDER"] = "true"
ok("RENDER=true hard-blocks even with opt-in", rs.seam("DOC_SCAN_FAKE") == "" and rs.looks_like_production())
os.environ.pop("RENDER")
for v in ("production", "prod", "PRODUCTION"):
    os.environ["SSC_ENVIRONMENT"] = v
    ok(f"SSC_ENVIRONMENT={v} hard-blocks even with opt-in", rs.seam("DOC_SCAN_FAKE") == "")
os.environ["SSC_ENVIRONMENT"] = "preprod"
ok("preprod is not treated as production", rs.seam("DOC_SCAN_FAKE") == "/tmp/synthetic.json")
os.environ.pop("SSC_ENVIRONMENT")
try:
    rs.seam("NOT_A_SEAM")
    ok("unregistered seam name raises", False)
except KeyError:
    ok("unregistered seam name raises", True)

# --- warning names only, never values ---
import logging, io  # noqa: E402
buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger().addHandler(h)
clean_env()
os.environ["GOOGLE_OAUTH_FAKE_VERIFY"] = "1"
os.environ["SSC_MAIL_FAKE_DIR"] = "/synthetic/secret-path-9X"
names = rs.warn_if_ignored()
logging.getLogger().removeHandler(h)
ok("ignored seams are reported by name", set(names) == {"GOOGLE_OAUTH_FAKE_VERIFY", "SSC_MAIL_FAKE_DIR"})
ok("warning never contains a value", "secret-path-9X" not in buf.getvalue())
os.environ[rs.ALLOW_ENV] = "1"
ok("no warning when opted in", rs.warn_if_ignored() == [])

# --- SSO bypass is actually closed: a forged claim is NOT accepted when the seam is inert ---
clean_env()
os.environ["GOOGLE_OAUTH_FAKE_VERIFY"] = "1"          # stray var, no opt-in
forged = base64.urlsafe_b64encode(json.dumps({"email": "x@example.invalid", "email_verified": True}).encode()).decode().rstrip("=")
try:
    import auth_google
    import requests

    def _no_network(*a, **k):
        raise RuntimeError("network blocked in test")
    real_post, requests.post = requests.post, _no_network
    try:
        ok("forged SSO code rejected when seam inert", auth_google._verify_google_code(forged) is None)
    finally:
        requests.post = real_post
    os.environ[rs.ALLOW_ENV] = "1"
    claims = auth_google._verify_google_code(forged)
    ok("same code accepted only when explicitly opted in (test mode)", bool(claims) and claims.get("email") == "x@example.invalid")
except ImportError as e:
    ok(f"auth_google importable for behaviour check ({e})", False)

# --- static: no module reads a seam variable directly; render.yaml never sets one ---
clean_env()
pat = re.compile(r"""environ(?:\.get\(|\[)\s*['"](%s)['"]""" % "|".join(rs.SEAM_VARS))
offenders = []
for p in ROOT.glob("*.py"):
    if p.name == "runtime_seams.py":
        continue
    for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        if pat.search(line) and not line.lstrip().startswith("#"):
            offenders.append(f"{p.name}:{i}")
ok("no production module reads a seam env var directly", not offenders)
if offenders:
    print("    offenders:", ", ".join(offenders))
rend = (ROOT / "render.yaml").read_text(encoding="utf-8")
setting = [n for n in rs.SEAM_VARS + (rs.ALLOW_ENV,) if re.search(r"^\s*-?\s*key:\s*%s\b" % n, rend, re.M)]
ok("render.yaml sets no seam or opt-in variable", not setting)
dock = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ok("Dockerfile sets no seam or opt-in variable", not any(n in dock for n in rs.SEAM_VARS + (rs.ALLOW_ENV,)))

if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: test seams are inert outside an opted-in test run")
