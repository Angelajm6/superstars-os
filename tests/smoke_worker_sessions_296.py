"""Static guard for #296's worker-clock authorization contract.

Run with: python tests/smoke_worker_sessions_296.py
No production database or PII is opened by this test.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
server = (ROOT / "server.py").read_text(encoding="utf-8")
worker_app = (ROOT / "worker-app.html").read_text(encoding="utf-8")
schema = (ROOT / "apply_worker_session_security_296.py").read_text(encoding="utf-8")


def expect(label, condition):
    if not condition:
        raise AssertionError(label)
    print(f"PASS: {label}")


expect("server-recorded worker session table", "CREATE TABLE IF NOT EXISTS worker_app_session" in schema)
expect("worker login mints strong opaque token", "secrets.token_urlsafe(32)" in server)
expect("clock-in validates worker session", "_worker_session(conn, session_token" in server)
expect("clock actions do not accept a client geofence bypass", "bypass_geofence" not in server)
expect("worker app sends the session token", "session_token: sessionData.sessionToken" in worker_app)
expect("worker app has no testing bypass", "TESTING_BYPASS" not in worker_app)
