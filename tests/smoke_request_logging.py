#!/usr/bin/env python3
"""Guard for metadata-only request logging (no DB, no secrets, no server boot).

Proves request bodies and credential/name-bearing URL segments never reach the log,
using synthetic values only (per CLAUDE.md PII rule).
"""
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402
import request_logging  # noqa: E402

FAILS = []


def ok(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# --- static: server.py wires the module and no longer reads bodies in a log hook ---
server_src = (ROOT / "server.py").read_text(encoding="utf-8")
ok("server.py applies request_logging", "request_logging.apply_request_logging(app)" in server_src)
ok("server.py has no body-logging hook", "_redact_pii" not in server_src and "| body: " not in server_src)

# --- behaviour on a minimal app ---
buf = io.StringIO()
handler = logging.StreamHandler(buf)
handler.setLevel(logging.INFO)
root = logging.getLogger()
root.addHandler(handler)
old_level = root.level
root.setLevel(logging.INFO)

app = Flask("t_reqlog")
request_logging.apply_request_logging(app)


@app.route("/<path:p>", methods=["GET", "POST"])
def _any(p):
    return "ok"


c = app.test_client()
SECRETS = ["synthetic.sender@example.invalid", "SYNTHETIC-BODY-TEXT-9X", "Synthetic Subject Line 9X",
           "tok_SYNTHETIC_CLIENT_TOKEN", "Synthetic_Workername", "9X-qs-secret"]

c.post("/api/anything", json={"sender": SECRETS[0], "body": SECRETS[1], "subject": SECRETS[2],
                              "password": "synthetic-pw", "pin": "1234"})
c.post("/api/raw", data="raw " + SECRETS[1], content_type="text/plain")
c.get("/p/" + SECRETS[3])
c.get("/api/p/" + SECRETS[3] + "/sign")
c.get("/i/" + SECRETS[3])
c.get("/d/" + SECRETS[3])
c.get("/worker-files/E-00001_" + SECRETS[4] + "/face.jpg")
c.get("/api/x?q=" + SECRETS[5])

out = buf.getvalue()
root.removeHandler(handler)
root.setLevel(old_level)

for s in SECRETS + ["synthetic-pw", "1234"]:
    ok(f"log never contains synthetic value '{s[:8]}…'", s not in out)
ok("method and path are logged", "POST /api/anything" in out)
ok("status is logged", "Response: 200" in out)
ok("body size is logged, not content", "body_bytes=" in out)
ok("token paths are masked", "/p/<redacted>" in out and "/api/p/<redacted>" in out
   and "/i/<redacted>" in out and "/d/<redacted>" in out)
ok("worker-files path is masked", "/worker-files/<redacted>" in out)

ok("sanitize_path leaves ordinary paths alone",
   request_logging.sanitize_path("/api/projects/FR-BX-001/dcr") == "/api/projects/FR-BX-001/dcr")
ok("sanitize_path does not mask look-alike prefixes",
   request_logging.sanitize_path("/proposals/12") == "/proposals/12")

if FAILS:
    print(f"FAIL: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("PASS: request logging is metadata-only")
