#!/usr/bin/env python3
"""Worker-app photos actually reach the job. Behavioral guard.

Before this, the worker app's Send button only bumped a local counter: every photo was
thrown away while the screen said "Photos upload directly". Boots the real app on an
ISOLATED database and data root. Proves:

  * POST /api/worker/photos needs a worker session on an OPEN shift: no token, a login
    without a started shift, and an ended shift -> 401; nothing stored
  * a valid photo lands in field_photos on the SHIFT's job (a project_code in the form
    is ignored), unassigned, worker_id set, caption "Scaffold 2 · Pointing", display +
    thumb files written, and the office sees it in the job's Field Photos list
  * where / scope outside the app's buttons -> 400; not an image -> 400; too large ->
    413; the per-day cap -> 429; none of these store a row
  * the log line carries the job and photo id only (no worker name, no employee id)
  * the app: uploadPhoto() posts to the endpoint, counts a photo only when the server
    returns a stored id (the offline service worker answers 200 {error:'offline'}),
    keeps the photo for a retry, EN + ES strings without possessive apostrophes; the
    service-worker cache version moved so phones fetch the new page
Run:  venv/bin/python tests/smoke_worker_photos.py
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="ssc_wphoto_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge, purge_users  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
(Path(_TMP) / "dataroot").mkdir(parents=True, exist_ok=True)
os.environ["SSC_DATA_ROOT"] = str(Path(_TMP) / "dataroot")
os.environ.setdefault("SSC_STRICT_ROUTES", "0")

from PIL import Image  # noqa: E402

import auth  # noqa: E402
import db_layer  # noqa: E402
import server  # noqa: E402  (boots the whole app)

TAG = "wphoto"
PW = "WorkerPhotoSmoke2026"
EID, WID, PIN, NAME = "E-97101", "W-9710", "9710", "Wphoto Smoke Mason"
P, OTHER = "WPH-P", "WPH-OTHER"
SITE = (40.8083, -73.9162)
API = "/api/worker/photos"


def _db():
    return db_layer.connect(pragma_fk=False)


def _jpeg(w=640, h=480) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (150, 90, 60)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _cleanup_rows(conn):
    for sql, args in (("DELETE FROM field_photos WHERE project_code IN (?, ?)", (P, OTHER)),
                      ("DELETE FROM worker_app_session WHERE employee_id = ?", (EID,)),
                      ("DELETE FROM sign_in_log WHERE employee_id = ?", (EID,)),
                      ("DELETE FROM project_assignments WHERE employee_id = ?", (EID,)),
                      ("DELETE FROM employees WHERE employee_id = ?", (EID,))):
        try:
            conn.execute(sql, args)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
    purge(conn, "projects", "project_code", [P, OTHER])


def _seed():
    conn = _db()
    try:
        _cleanup_rows(conn)
        try:
            conn.execute("ALTER TABLE employees ADD COLUMN pin TEXT")   # the fixture may predate employees.pin
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        today = date.today().isoformat()
        conn.execute("INSERT INTO employees (employee_id, worker_id, name, trade, pin, created_at, updated_at) "
                     "VALUES (?, ?, ?, 'mason', ?, ?, ?)", (EID, WID, NAME, PIN, today, today))
        for code in (P, OTHER):
            conn.execute("INSERT INTO projects (project_code, name, address, status, site_lat, site_lng, geofence_m) "
                         "VALUES (?, ?, 'x', 'active', ?, ?, 200)", (code, f"Wphoto {code}", *SITE))
        conn.execute("INSERT INTO project_assignments (project_code, employee_id, start_date, status) "
                     "VALUES (?, ?, ?, 'active')", (P, EID, today))
        email = f"{TAG}.admin@example.com"
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("INSERT INTO users (email, password_hash, role, full_name, is_active, status, must_reset_password, is_system) "
                     "VALUES (?, ?, 'admin', 'Smoke admin', 1, 'active', 0, 1)", (email, auth.hash_password(PW)))
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    conn = _db()
    try:
        _cleanup_rows(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        conn.commit()
        purge_users(conn, uids)
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(_TMP, ignore_errors=True)


def _rows():
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT project_code, drop_id, worker_id, caption, uploaded_by_uid, file_path, thumb_path "
            "FROM field_photos WHERE project_code IN (?, ?) ORDER BY id", (P, OTHER)).fetchall()]
    finally:
        conn.close()


def _post(c, token, *, location="S2", scope="pointing", data=None, extra=None, name="photo.jpg"):
    form = {"session_token": token or "", "employee_id": EID, "location": location, "scope": scope}
    form.update(extra or {})
    if data is not False:
        form["photo"] = (io.BytesIO(_jpeg() if data is None else data), name)
    return c.post(API, data=form, content_type="multipart/form-data")


def main() -> int:
    _seed()
    passed = 0
    c = server.app.test_client()   # the worker app has no dashboard session

    # ---- session gates
    assert _post(c, "").status_code == 401
    r = c.post("/api/worker/login", json={"phone_or_pin": PIN, "latitude": SITE[0], "longitude": SITE[1]})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    token = r.get_json()["data"]["session_token"]
    assert _post(c, token).status_code == 401, "a login without a started shift cannot upload"
    r = c.post("/api/worker/session/start", json={"employee_id": EID, "project_code": P, "session_token": token})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert _rows() == []
    passed += 1; print("  session gates ok")

    # ---- a real upload lands on the shift's job, visible to the office
    buf = io.StringIO(); h = logging.StreamHandler(buf); h.setLevel(logging.DEBUG)
    root = logging.getLogger(); old_level = root.level; root.setLevel(logging.INFO); root.addHandler(h)
    try:
        r = _post(c, token, extra={"project_code": OTHER})
    finally:
        root.removeHandler(h); root.setLevel(old_level)
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    d = r.get_json()["data"]
    assert d["id"] and d["project_code"] == P and d["caption"] == "Scaffold 2 · Pointing" and d["sent_today"] == 1, d
    rows = _rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["project_code"] == P, "a form project_code never redirects the photo"
    assert row["drop_id"] is None and row["worker_id"] == EID and row["uploaded_by_uid"] is None
    assert row["caption"] == "Scaffold 2 · Pointing"
    data_root = Path(os.environ["SSC_DATA_ROOT"])
    assert (data_root / row["file_path"]).is_file() and (data_root / row["thumb_path"]).is_file(), "display + thumb written"
    log = buf.getvalue()
    assert f"worker photo stored project={P} id={d['id']}" in log, log
    assert NAME not in log and EID not in log, "no worker identity in the log"
    admin = server.app.test_client()
    assert admin.post("/api/auth/login", json={"email": f"{TAG}.admin@example.com", "password": PW}).status_code == 200
    lst = admin.get(f"/api/projects/{P}/photos")
    assert lst.status_code == 200, lst.status_code
    import json
    assert "Scaffold 2 · Pointing" in json.dumps(lst.get_json(), ensure_ascii=False), "office sees it in Field Photos"
    un = admin.get(f"/api/projects/{P}/photos?drop_id=__unassigned__&group=all").get_json()
    def _photos(o):
        if isinstance(o, dict):
            if "thumb_url" in o and "id" in o:
                yield o
            for v in o.values():
                yield from _photos(v)
        elif isinstance(o, list):
            for v in o:
                yield from _photos(v)
    assert any(x["id"] == d["id"] and x["drop_id"] is None for x in _photos(un)), "it waits in the Unassigned tray"
    passed += 1; print("  upload ok")

    # ---- refusals store nothing
    assert _post(c, token, location="S9").status_code == 400
    assert _post(c, token, scope="paint").status_code == 400
    assert _post(c, token, data=False).status_code == 400
    assert _post(c, token, data=b"not an image at all", name="x.jpg").status_code == 400
    big = _post(c, token, data=b"\xff" * (server._WORKER_PHOTO_MAX_BYTES + 10))
    assert big.status_code == 413, big.status_code
    cap = server._WORKER_PHOTO_DAILY_CAP
    server._WORKER_PHOTO_DAILY_CAP = 1
    try:
        assert _post(c, token).status_code == 429, "daily cap"
    finally:
        server._WORKER_PHOTO_DAILY_CAP = cap
    assert len(_rows()) == 1, "no refused request stored a row"
    passed += 1; print("  refusals ok")

    # ---- worker videos (field_videos.py, accepted 2026-10-14): 60 s at most, a JPEG still for the grid
    import field_videos as fv
    import visibility
    mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 4000
    def _vpost(data=mp4, duration="12", poster=True, location="S2", scope="pointing"):
        form = {"session_token": token, "employee_id": EID, "location": location, "scope": scope, "duration": duration,
                "video": (io.BytesIO(data), "video.mp4")}
        if poster:
            form["poster"] = (io.BytesIO(_jpeg(320, 240)), "poster.jpg")
        return c.post("/api/worker/videos", data=form, content_type="multipart/form-data")
    assert _vpost(duration="75").status_code == 400, "longer than a minute"
    assert _vpost(duration="0").status_code == 400
    assert _vpost(data=b"not a video" * 50).status_code == 400, "the bytes must be a video, whatever the name says"
    assert _vpost(poster=False).status_code == 400
    assert _vpost(location="S9").status_code == 400
    old_max, old_cap, old_room = fv.MAX_BYTES, fv.DAILY_CAP, fv.has_room
    try:
        fv.MAX_BYTES = 100
        assert _vpost().status_code == 413
        fv.MAX_BYTES = old_max
        fv.has_room = lambda size: False
        assert _vpost().status_code == 507, "a nearly full disk refuses videos (photos keep working)"
    finally:
        fv.MAX_BYTES, fv.has_room = old_max, old_room
    assert len(_rows()) == 1, "no refused video stored a row"
    r = _vpost()
    assert r.status_code == 201, r.get_data(as_text=True)[:300]
    vid = r.get_json()["data"]
    assert vid["project_code"] == P and vid["caption"] == "Scaffold 2 · Pointing" and vid["videos_today"] == 1
    conn = _db()
    try:
        vrow = dict(conn.execute("SELECT mime, duration_s, file_path, thumb_path, file_size, worker_id, drop_id FROM field_photos WHERE id = ?",
                                 (vid["id"],)).fetchone())
    finally:
        conn.close()
    assert vrow["mime"] == "video/mp4" and vrow["duration_s"] == 12 and vrow["file_size"] == len(mp4) and vrow["worker_id"] == EID
    assert (data_root / vrow["file_path"]).read_bytes() == mp4, "stored as sent, no re-encoding"
    assert (data_root / vrow["thumb_path"]).read_bytes()[:2] == b"\xff\xd8", "the still is a JPEG"
    fv.DAILY_CAP = 1
    try:
        assert _vpost().status_code == 429, "videos a day"
    finally:
        fv.DAILY_CAP = old_cap
    assert _post(c, token).status_code == 201, "the video cap leaves photos alone"
    th = admin.get(f"/api/field-photos/{vid['id']}/thumb")
    assert th.status_code == 200 and th.mimetype == "image/jpeg", "a video thumbnail is served as an image"
    fl = admin.get(f"/api/field-photos/{vid['id']}/file")
    assert fl.status_code == 200 and fl.mimetype == "video/mp4"
    part = admin.get(f"/api/field-photos/{vid['id']}/file", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.data) == 100, "phones seek through videos with range requests"
    listed = [x for x in _photos(admin.get(f"/api/projects/{P}/photos?drop_id=__unassigned__&group=all").get_json()) if x["id"] == vid["id"]]
    assert listed and listed[0]["kind"] == "video" and listed[0]["duration_s"] == 12
    assert "file_path" not in listed[0] and "thumb_path" not in listed[0]
    # videos stay inside the office: sharing refused, and never visible to a client even if a share row exists
    sh = admin.post(f"/api/field-photos/{vid['id']}/share", json={"audience": "client", "on": True})
    assert sh.status_code == 409, sh.status_code
    conn = _db()
    try:
        conn.execute("INSERT INTO item_visibility (item_type, item_id, audience, shared_by, shared_at) VALUES ('photo', ?, 'client', NULL, '2026-10-14 09:00:00')", (vid["id"],))
        conn.commit()
        assert vid["id"] not in visibility.client_visible_photo_ids(conn, P)
        assert not visibility.photo_visible_to_client(conn, vid["id"], P)
        conn.execute("DELETE FROM item_visibility WHERE item_type='photo' AND item_id = ?", (vid["id"],))
        conn.commit()
    finally:
        conn.close()
    st = fv.storage()
    assert set(st) == {"total", "used", "free", "used_pct", "warn"} and 0 <= st["used_pct"] <= 100
    passed += 1; print("  videos ok")
    n_rows = len(_rows())

    # ---- after sign-out the token is dead
    r = c.post("/api/worker/session/end", json={"employee_id": EID, "session_token": token})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert _post(c, token).status_code == 401
    assert _vpost().status_code == 401
    assert len(_rows()) == n_rows
    passed += 1; print("  sign-out ok")

    # ---- the app itself
    app = (ROOT / "worker-app.html").read_text(encoding="utf-8")
    fn = app[app.index("async function uploadPhoto()"):app.index("function uploadVideo()")]
    assert "fetch('/api/worker/photos'" in fn and "new FormData()" in fn
    assert "body.data && body.data.id" in fn, "counted only when the server stored it"
    assert fn.index("body.data && body.data.id") < fn.index("sessionData.photoCount++"), "count after the server confirms"
    assert "if (!ok) {" in fn and "return;" in fn[fn.index("if (!ok) {"):], "a failure keeps the photo for a retry"
    for key in ("photo_sending", "photo_sent", "photo_retry", "photo_failed", "photo_offline", "photo_session", "photo_limit", "photo_missing"):
        vals = re.findall(key + r': "([^"]*)"', app)
        assert len(vals) == 2, (key, vals)   # EN + ES
        assert not any(re.search(r"\w'\w", v) for v in vals), (key, "no possessive apostrophes in i18n strings")
    for loc in ("S1", "S2", "S3", "S4", "S5", "ROOF", "INT", "OUT"):
        assert f"data-loc=\"{loc}\"" in app and loc in server._WORKER_PHOTO_LOCATIONS
    for sc in re.findall(r"selectScope\('([a-z]+)'", app):
        assert sc in server._WORKER_PHOTO_SCOPES, sc
    vfn = app[app.index("function uploadVideo()"):app.index("function flashPhotoSent(")]
    assert "xhr.open('POST', '/api/worker/videos')" in vfn and "fd.append('poster'" in vfn and "fd.append('duration'" in vfn
    assert "body && body.data && body.data.id) return done(true);" in vfn, "counted only when the server stored it"
    assert vfn.index("if (!ok) {") < vfn.index("sessionData.photoCount++"), "a failed video is kept for a retry, not counted"
    assert "const VIDEO_MAX_S = 60;" in app and "if (sec >= VIDEO_MAX_S) stopRecording();" in app, "recording stops by itself at 1:00"
    assert 'id="mode-video"' in app and "function shutterPress()" in app and 'id="review-video"' in app
    for key in ("mode_photo", "mode_video", "confirm_video", "record_again", "use_video", "video_sending", "video_sent", "video_failed",
                "video_limit", "video_big", "video_full", "video_left", "video_unsupported"):
        vals = re.findall(key + r': "([^"]*)"', app)
        assert len(vals) == 2, (key, vals)
        assert not any(re.search(r"\w'\w", v) for v in vals), (key, "no possessive apostrophes in i18n strings")
    sw = (ROOT / "worker-app-sw.js").read_text(encoding="utf-8")
    assert "v9-2026-10-08a" not in sw, "cache version moved for the video update"
    assert "v7-2026-05-06e" not in sw, "cache version bumped so phones fetch the fixed page"
    assert "error: 'offline'" in sw, "the offline answer this app must not count as sent"
    passed += 1; print("  app ok")

    print(f"PASS smoke_worker_photos ({passed} groups)")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_worker_photos: {e}")
        rc = 1
    finally:
        _cleanup()
    sys.exit(rc)
