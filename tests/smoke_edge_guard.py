#!/usr/bin/env python3
"""Only traffic that came through Cloudflare (edge_guard.py, 2026-10-30).

Proves (no database needed):
  * off when SSC_EDGE_SECRET is unset: every request passes
  * on: no header / a wrong header -> 403; the right header -> through; /api/health always answers
  * the guard runs before the login gate (server.py registers it first)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402

import edge_guard  # noqa: E402


def main() -> int:
    os.environ.pop("SSC_EDGE_SECRET", None)
    app = Flask("edge")
    edge_guard.apply_edge_guard(app)
    app.add_url_rule("/x", "x", lambda: "ok")
    app.add_url_rule("/api/health", "h", lambda: "healthy")
    c = app.test_client()
    assert c.get("/x").status_code == 200, "off when unset"
    os.environ["SSC_EDGE_SECRET"] = "s3cret-from-cloudflare"
    try:
        assert c.get("/x").status_code == 403
        assert c.post("/x", headers={"X-SSC-Edge": "nope"}).status_code == 403
        assert c.get("/x", headers={"X-SSC-Edge": "s3cret-from-cloudflare"}).status_code == 200
        assert c.get("/api/health").status_code == 200, "Render's health check skips Cloudflare"
    finally:
        os.environ.pop("SSC_EDGE_SECRET", None)
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 0 < src.index("edge_guard.apply_edge_guard(app)") < src.index("apply_auth_gate(app)")
    print("smoke_edge_guard: 1 group passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
