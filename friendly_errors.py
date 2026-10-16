"""Friendly 403 / 404 pages for people, raw JSON for programs.

Many routes answer a refusal with `jsonify({"error": "forbidden"}), 403` and the global
404 handler answers `{"error": "Endpoint not found"}`. That is right for the app's own
fetch() calls and for /api/*, but a person who types a wrong address or opens a page their
role cannot see should get a page that says so and offers a way back, not a line of JSON.

One after_request hook rewrites such a response ONLY when all of these hold:
  * the status is 403 or 404 and the body is JSON,
  * the request is a browser navigation (its Accept header asks for text/html — fetch(),
    XHR and curl do not),
  * the path is not under /api/, /files/ or /project-files/ (assets and data stay JSON).
Everything else is returned untouched, so no program contract changes.
"""
from __future__ import annotations

from flask import request

_COPY = {
    403: ("You don't have access to this page",
          "Your account can't open this one. If you think it should, ask an admin to check your access."),
    404: ("We can't find that page",
          "The address may be mistyped, or the page may have moved. Head back home and pick it from the menu."),
}

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · Superstars Contracting</title>
<link rel="stylesheet" href="/files/static/fonts/typography.css?v=1014">
<link rel="stylesheet" href="/files/static/css/widgets.css?v=1007">
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{min-height:100vh;display:grid;place-items:center;background:var(--canvas,#fafbfd);color:var(--ink,#222633);
        font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:24px;}}
  .card{{width:100%;max-width:460px;background:#fff;border:1px solid var(--line,#eceef3);border-radius:14px;
         padding:32px 28px;text-align:center;box-shadow:0 2px 12px rgba(20,22,28,.05);}}
  .mark{{width:44px;height:44px;object-fit:contain;margin-bottom:18px;}}
  .code{{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted,#8c92a0);margin-bottom:8px;}}
  h1{{font-size:22px;font-weight:600;line-height:1.25;margin-bottom:10px;}}
  p{{font-size:14px;line-height:1.55;color:var(--muted,#8c92a0);margin-bottom:24px;}}
  .row{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;}}
  .row a,.row button{{font:inherit;text-decoration:none;}}
</style>
</head>
<body>
<main class="card">
  <img class="mark" src="/files/static/brand/mark.svg" alt="Superstars Contracting">
  <div class="code">Error {status}</div>
  <h1>{title}</h1>
  <p>{detail}</p>
  <div class="row">
    <a class="btn-primary" id="home" href="/projects">Go to Home</a>
    <button class="btn-ghost" type="button" onclick="history.length>1?history.back():location.assign('/projects')">Go back</button>
  </div>
</main>
<script>
  // Staff land on the Company dashboard, everyone else on their Home. A failed lookup keeps /projects.
  fetch('/api/auth/me',{{credentials:'same-origin'}}).then(function(r){{return r.ok?r.json():null;}}).then(function(j){{
    var u=j&&(j.user||j.data||j), role=u&&u.role;
    if(role==='admin'||role==='c_suite') document.getElementById('home').href='/company#overview';
  }}).catch(function(){{}});
</script>
</body>
</html>
"""

_SKIP_PREFIXES = ("/api/", "/files/", "/project-files/", "/worker-files/")


def _wants_page() -> bool:
    if request.path.startswith(_SKIP_PREFIXES):
        return False
    return "text/html" in (request.headers.get("Accept") or "")


def install(app) -> None:
    @app.after_request
    def _friendly_error_page(resp):
        if resp.status_code not in _COPY or resp.mimetype != "application/json" or not _wants_page():
            return resp
        title, detail = _COPY[resp.status_code]
        resp.set_data(_PAGE.format(title=title, detail=detail, status=resp.status_code))
        resp.mimetype = "text/html"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        resp.headers["Cache-Control"] = "no-store"
        return resp
