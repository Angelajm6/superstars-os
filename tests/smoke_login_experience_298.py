#!/usr/bin/env python3
"""Static UX guard for password visibility and honest reset guidance (#298)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
login = (ROOT / "login.html").read_text(encoding="utf-8")
set_password = (ROOT / "set_password.html").read_text(encoding="utf-8")

assert 'id="toggle-password"' in login
assert "pwEl.type = revealing ? 'text' : 'password'" in login
assert 'id="forgot-password"' in login
assert "contact your company administrator" in login
assert "Self-service reset must not be added" in login
assert 'id="toggle-pw"' in set_password
assert 'id="toggle-pw2"' in set_password
assert "togglePassword(pw,togglePw" in set_password
assert "togglePassword(pw2,togglePw2" in set_password
print("PASS: #298 polished password visibility and safe reset guidance")
