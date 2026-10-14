"""Worker-app videos (accepted 2026-10-14).

A worker records a short video in the worker app (Photo / Video switch on the same camera) and
sends it with the same "where" and "scope" as a photo. It lands in the job's Field Photos next to
the photos: same table (field_photos), mime 'video/...', file_path = the video, thumb_path = a JPEG
still the app grabbed when recording started (so the grid needs no video decoding on the server).

Limits (videos are far bigger than photos and the server keeps every job's files on one disk):
  * MAX_SECONDS 60 per video (the app stops recording by itself; the server re-checks the length
    the app reports), MAX_BYTES per file, DAILY_CAP videos per worker per job per day
  * MIN_FREE_BYTES: a video is refused when the disk is nearly full, so photos keep working
  * the job's "photos" worker switch covers videos too
Videos stay inside the office: they are never shareable to the client portal (visibility.py
excludes them), and PDFs never read field photos. Logs carry ids only.
"""
from __future__ import annotations

import shutil
from typing import Optional

import db_layer
import ssc_paths

MAX_SECONDS = 60
MAX_BYTES = 40 * 1024 * 1024        # a 1-minute recording at the app's bitrate is ~10 MB; room for phones that ignore it
DAILY_CAP = 10                      # videos per worker per job per day (photos keep their own 300)
MIN_FREE_BYTES = 300 * 1024 * 1024  # keep this much disk free for photos and documents
WARN_USED_PCT = 80                  # the company dashboard says so from here
MIMES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}


def is_video(mime) -> bool:
    return str(mime or "").lower().startswith("video/")


def kind_of(data: bytes) -> Optional[str]:
    """The container from the first bytes (never trust the browser's label alone):
    'video/mp4' / 'video/quicktime' (ISO base media: 'ftyp' at offset 4) or 'video/webm' (EBML)."""
    head = data[:16]
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/quicktime" if head[8:10] == b"qt" else "video/mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    return None


def clean_duration(raw) -> int:
    """Seconds the app reports, 1..MAX_SECONDS (+2 s grace for the stop button), else ValueError."""
    try:
        s = int(round(float(raw)))
    except (TypeError, ValueError):
        raise ValueError("video length missing")
    if s < 1:
        raise ValueError("video is too short")
    if s > MAX_SECONDS + 2:
        raise ValueError(f"videos are {MAX_SECONDS} seconds at most")
    return min(s, MAX_SECONDS)


def _has_column(conn, table, column) -> bool:
    if db_layer.is_postgres():
        return bool(conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                                 "AND table_name = ? AND column_name = ?", (table, column)).fetchone())
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone():
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure(conn) -> None:
    """Idempotent: field_photos.duration_s (seconds; NULL for photos)."""
    if not _has_column(conn, "field_photos", "id"):
        return   # no field_photos table yet (a fixture that migrates later)
    if not _has_column(conn, "field_photos", "duration_s"):
        conn.execute("ALTER TABLE field_photos ADD COLUMN duration_s INTEGER")
        conn.commit()


def storage() -> dict:
    """Disk use where job files live: {total, used, free, used_pct, warn}. Never raises."""
    try:
        u = shutil.disk_usage(str(ssc_paths.data_root()))
        pct = round(u.used * 100 / u.total) if u.total else 0
        return {"total": u.total, "used": u.used, "free": u.free, "used_pct": pct, "warn": pct >= WARN_USED_PCT}
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "used_pct": 0, "warn": False}


def has_room(size: int) -> bool:
    s = storage()
    return not s["total"] or s["free"] - size >= MIN_FREE_BYTES
