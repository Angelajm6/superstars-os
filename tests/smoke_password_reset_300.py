from pathlib import Path
root=Path(__file__).resolve().parents[1]
auth=(root/'auth.py').read_text(); reset=(root/'password_reset.py').read_text(); login=(root/'login.html').read_text()
assert 'password_reset_token' in reset and 'token_hash' in reset and 'used_at' in reset
assert 'secrets.token_urlsafe(32)' in reset and 'hashlib.sha256' in reset
assert 'SSC_RESET_SMTP_PASSWORD' in reset and 'SSC_PUBLIC_BASE_URL' in reset
assert 'If an active account matches that email' in reset
assert 'destroy_all_user_sessions(uid)' in reset
assert '"/forgot-password"' in auth and '"/reset-password"' in auth
assert "window.location.href = '/forgot-password'" in login
assert '.password-wrap input' in login
print('PASS #300 secure password reset contract')
