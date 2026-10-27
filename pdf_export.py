"""Headless-browser PDF rendering (#288 Cloud M2: engine-selectable).

Two engines, ONE code path:
  * edge      — headless Microsoft Edge. The Windows default and today's
                production path, byte-for-byte (same binary discovery, same
                flags, same quirk handling as before #288).
  * chromium  — headless Chromium/Chrome for the Linux cloud host (M4 sets
                it explicitly). Binary from SSC_CHROMIUM_PATH, else a PATH
                lookup over chromium / chromium-browser / google-chrome /
                chrome, else the standard Windows Chrome install dirs (so
                the workstation can run engine-parity checks).

Selection: SSC_PDF_ENGINE env ('edge' | 'chromium'), read per call. Unset ->
edge — a Windows workstation with no env change behaves exactly as before.

Why a browser and not WeasyPrint: WeasyPrint requires GTK on Windows; GTK
won't install on this workstation (Windows Home, no MSYS2 toolchain).
Headless Chromium-family browsers print self-contained HTML deterministically
on both OSes.

Usage:
  from pdf_export import render_html_to_pdf, PDFExportError
  result = render_html_to_pdf(html_path, pdf_path)
  if result["ok"]:
      ...
  else:
      log warning, surface result["error"] to UI

Caller policy:
  This module never raises on rendering failures — it returns
  {"ok": False, "error": ...} so the caller (DCR issuance) decides whether
  to fail the whole issue or just warn + continue. The only exception
  raised is PDFExportError when the selected engine's browser is not
  installed / not found (a setup-time problem, not a runtime one).

Result-shape compatibility note: the browser path is still returned under
the key "edge_path" regardless of engine — that key is on the server's
response scrub list (#247) and several callers read it; renaming it would
re-open a path-leak review for zero benefit. An "engine" key rides along.
"""
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path


def _best_effort_rmtree(path, attempts=5, delay=0.2):
    """Edge holds file handles in its profile dir briefly after subprocess
    exits on Windows, racing tempfile.TemporaryDirectory's cleanup. Try
    several times with backoff, then give up — temp dir contents are tiny
    and Windows will reclaim %TEMP% eventually."""
    for i in range(attempts):
        try:
            shutil.rmtree(str(path))
            return True
        except (OSError, PermissionError):
            time.sleep(delay * (i + 1))
    # last-ditch: ignore_errors so we don't fail the whole render on cleanup
    shutil.rmtree(str(path), ignore_errors=True)
    return not Path(path).exists()

# Standard Edge install locations on Windows. The 32-bit-suffix path is the
# default for Stable, but Insider / per-user installs can land in the other.
EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

# Chromium-family binary names for the PATH lookup (Linux first, then the
# generic Chrome names), plus the standard Windows Chrome dirs so the
# workstation can exercise the chromium engine for parity checks.
CHROMIUM_BINARY_NAMES = ("chromium", "chromium-browser", "google-chrome", "chrome")
CHROME_WIN_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]

ENGINES = ("edge", "chromium")

CHROMIUM_CONTAINER_FLAGS = [
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--no-first-run",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--mute-audio",
]

# One browser at a time: on a small host two renders starve each other and both time out (a person
# pressing Send again while the first PDF is still being made). The next render waits its turn.
_RENDER_LOCK = threading.Lock()


class PDFExportError(Exception):
    """Raised when the selected engine's browser cannot be found (setup-time)."""


def active_engine() -> str:
    """The engine this process should use: SSC_PDF_ENGINE, default 'edge'.
    Read per call (same discipline as ssc_paths #287) — a bad value raises
    loudly rather than silently falling back to a browser nobody chose."""
    v = (os.environ.get("SSC_PDF_ENGINE") or "").strip().lower() or "edge"
    if v not in ENGINES:
        raise PDFExportError(
            f"SSC_PDF_ENGINE={v!r} is not a valid engine (expected one of {ENGINES})")
    return v


def find_edge_executable():
    """Return the Path to msedge.exe, or raise PDFExportError if not found.
    (Kept under its historical name — pre-#288 importers still call it.)"""
    for p in EDGE_PATHS:
        if p.exists():
            return p
    raise PDFExportError(
        "Microsoft Edge not found. Checked: "
        + " | ".join(str(p) for p in EDGE_PATHS)
    )


def find_chromium_executable():
    """Locate a Chromium-family browser. SSC_CHROMIUM_PATH wins when set (and
    MUST exist — a configured-but-wrong path is a setup error to surface, never
    something to silently fall past); else PATH lookup; else Windows Chrome."""
    explicit = (os.environ.get("SSC_CHROMIUM_PATH") or "").strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise PDFExportError(
            f"SSC_CHROMIUM_PATH is set but does not exist: {explicit}")
    for name in CHROMIUM_BINARY_NAMES:
        hit = shutil.which(name)
        if hit:
            return Path(hit)
    for p in CHROME_WIN_PATHS:
        if p.exists():
            return p
    raise PDFExportError(
        "No Chromium-family browser found. Set SSC_CHROMIUM_PATH, or install one of: "
        + ", ".join(CHROMIUM_BINARY_NAMES))


def find_browser_executable(engine=None):
    """(engine, Path) for the active — or explicitly requested — engine."""
    engine = engine or active_engine()
    if engine == "edge":
        return "edge", find_edge_executable()
    return "chromium", find_chromium_executable()


def engine_flags(engine, profile_dir, pdf_path, budget_ms=5000):
    """The full headless-print flag list for one render. EVERY engine-specific
    flag lives here and nowhere else.

    edge:     --headless=old — required for reliable --print-to-pdf on Edge
              (the newer mode sometimes exits 0 without flushing the PDF).
    chromium: plain --headless — modern Chrome/Chromium removed the old
              headless mode entirely; the new one prints reliably there.
              --no-sandbox — containerized Linux hosts (the M4 target) run
              without the kernel privileges Chromium's sandbox wants; a PDF
              render of OUR OWN self-contained file:// HTML carries no
              untrusted content, so the sandbox buys nothing here.
              The rest are the usual container flags (2026-10-26): Docker's 64MB
              /dev/shm, the zygote and Chromium's own background fetches
              (component updates, sync, variations) made a proposal render run
              past 30s on the 0.5-CPU Render host, so Send failed.
    """
    head = ["--headless=old"] if engine == "edge" else ["--headless", "--no-sandbox"] + CHROMIUM_CONTAINER_FLAGS
    return head + [
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--user-data-dir={profile_dir}",
        f"--virtual-time-budget={budget_ms}",
        f"--print-to-pdf={pdf_path}",
    ]


def render_html_to_pdf(html_path, pdf_path, timeout_sec=60, engine=None):
    """Render an HTML file to PDF using the active headless engine (#288).

    Args:
      html_path: Path or str — must exist on disk. Should be self-contained
                 (inline CSS, no external/network assets) so the render is
                 deterministic and doesn't depend on whether Edge can reach
                 the dashboard's static-file route.
      pdf_path:  Path or str — destination. Parent dirs are created.
      timeout_sec: hard cap on Edge process runtime (default 60s — generous
                   ceiling so a slow first-launch Edge doesn't hit the limit,
                   but well under any reasonable operator patience for the
                   finalize request to return). On timeout, subprocess.run
                   kills the Edge process via Popen.kill() (Python stdlib
                   contract) and we return ok=False so the caller WARNs and
                   finalize still returns 201 — the DCR is the source of
                   truth; the PDF is derivative and can be regenerated.

    Returns dict (shape unchanged from the Edge-only era — "edge_path" carries
    the browser path for EITHER engine; see the module docstring):
      {"ok": True,  "pdf_path": "<abs>", "edge_path": "<abs>", "size": int, "engine": str}
      {"ok": False, "edge_path": "<abs>", "error": "<reason>", "engine": str}

    Raises PDFExportError if the selected engine's browser isn't installed
    (setup problem).
    """
    html_path = Path(html_path).resolve()
    pdf_path = Path(pdf_path).resolve()
    if not html_path.exists():
        return {"ok": False, "error": f"HTML not found: {html_path}"}

    engine, browser = find_browser_executable(engine)

    # Fresh per-render profile dir. Without --user-data-dir the browser attaches
    # to any running instance and silently skips --print-to-pdf. With a FRESH
    # temp dir each invocation, we get a guaranteed new browser process.
    # Not using `with TemporaryDirectory(...)` — Edge holds file handles in the
    # profile briefly after subprocess.run() returns, racing the auto-cleanup
    # and crashing with WinError 32. mkdtemp + best-effort cleanup avoids that.
    profile_dir = tempfile.mkdtemp(prefix=f"dcr_{engine}_")
    try:
        cmd = [str(browser)] + engine_flags(engine, profile_dir, pdf_path) + [
            html_path.as_uri(),  # file:///C:/Users/...
        ]
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with _RENDER_LOCK:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout_sec
                )
        except subprocess.TimeoutExpired:
            return {"ok": False, "edge_path": str(browser), "engine": engine,
                    "error": f"{engine} timed out after {timeout_sec}s"}
        except Exception as e:
            return {"ok": False, "edge_path": str(browser), "engine": engine,
                    "error": f"{engine} subprocess failed: {e}"}

        stderr_tail = (result.stderr or "").strip()[-500:]
        stdout_tail = (result.stdout or "").strip()[-500:]
        if result.returncode != 0:
            return {
                "ok": False,
                "edge_path": str(browser),
                "engine": engine,
                "error": f"{engine} exited rc={result.returncode}: {stderr_tail or stdout_tail}",
            }
        # The browser sometimes writes the file just after subprocess.run()
        # returns (filesystem flush lag). Poll briefly before giving up.
        for _ in range(20):
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                break
            time.sleep(0.1)
        if not pdf_path.exists():
            return {"ok": False, "edge_path": str(browser), "engine": engine,
                    "error": f"{engine} exited 0 but no PDF written. stderr_tail: {stderr_tail}"}
        size = pdf_path.stat().st_size
        if size == 0:
            return {"ok": False, "edge_path": str(browser), "engine": engine,
                    "error": f"PDF written but is 0 bytes. stderr_tail: {stderr_tail}"}
        return {"ok": True, "pdf_path": str(pdf_path),
                "edge_path": str(browser), "size": size, "engine": engine}
    finally:
        _best_effort_rmtree(profile_dir)


def slugify_for_filename(s, fallback="unknown"):
    """Lightweight slugifier for filenames: keep alnum, replace spaces with
    hyphens, strip everything else. Not for URLs — for human-readable files."""
    if not s:
        return fallback
    import re
    out = re.sub(r"[^A-Za-z0-9 _-]", "", str(s))
    out = re.sub(r"\s+", "-", out).strip("-_")
    return out or fallback


if __name__ == "__main__":
    # Tiny CLI for ad-hoc verification:
    #   python pdf_export.py <html_in> <pdf_out>
    import sys
    if len(sys.argv) != 3:
        print("usage: pdf_export.py <html_in> <pdf_out>", file=sys.stderr)
        sys.exit(2)
    try:
        r = render_html_to_pdf(sys.argv[1], sys.argv[2])
    except PDFExportError as e:
        print(f"setup error: {e}", file=sys.stderr)
        sys.exit(3)
    if r["ok"]:
        print(f"ok: {r['pdf_path']} ({r['size']} bytes, edge={r['edge_path']})")
    else:
        print(f"failed: {r['error']}", file=sys.stderr)
        sys.exit(1)
