#!/usr/bin/env python3
"""#353 — Drawing Markup: photoreal building (Google Photorealistic 3D Tiles). Guard.

No network, no key. Proves:
  * building_3d.photoreal() switches on ONLY with a key AND the vendored Google logo AND a
    located building; otherwise it names the reason (no_key / no_logo / no_location) and the
    page keeps the massing view. When on, it hands the browser the key, the reference
    lat/lng (the origin of every local coordinate) and a ground guess on the ellipsoid.
  * the model payload carries the block
  * the vendored library is complete: every relative import resolves to a vendored file and
    the only bare imports are "three" / "three/addons/..." (mapped by the page's import map),
    plus the lazily-loaded vector-tile modules that the photoreal view never calls
  * the page declares the import map and loads the versioned module; the module anchors the
    tiles with the azimuth-pi reorientation and shows Google's logo + data credits
Run:  venv/bin/python tests/smoke_photoreal_3d_353.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import building_3d  # noqa: E402

GEOM = {"available": True, "lat": 40.8072, "lng": -73.9175, "ground_ft": 21.0}
LAZY_ONLY = {"@mapbox/vector-tile", "pbf", "pmtiles"}   # dynamic import() in unused plugins


def check_availability() -> None:
    old_key, old_logo = os.environ.get("SSC_GOOGLE_TILES_KEY"), building_3d.GOOGLE_LOGO_REL
    tmp = Path(tempfile.mkdtemp(prefix="ssc_pr353_")) / "logo.svg"
    try:
        os.environ.pop("SSC_GOOGLE_TILES_KEY", None)
        assert building_3d.photoreal(GEOM) == {"available": False, "reason": "no_key"}
        os.environ["SSC_GOOGLE_TILES_KEY"] = "test-browser-key"
        building_3d.GOOGLE_LOGO_REL = str(tmp)                  # absolute: joined as-is
        assert building_3d.photoreal(GEOM)["reason"] == "no_logo", "no logo -> no photoreal (Google policy)"
        tmp.write_text("<svg/>", encoding="utf-8")
        assert building_3d.photoreal({"available": False})["reason"] == "no_location"
        on = building_3d.photoreal(GEOM)
        assert on["available"] is True and on["key"] == "test-browser-key", on
        assert (on["lat"], on["lng"]) == (GEOM["lat"], GEOM["lng"])
        assert abs(on["ground_ellipsoid_m"] - (21.0 * 0.3048 + building_3d.GEOID_NYC_M)) < 0.01, on
    finally:
        building_3d.GOOGLE_LOGO_REL = old_logo
        if old_key is None:
            os.environ.pop("SSC_GOOGLE_TILES_KEY", None)
        else:
            os.environ["SSC_GOOGLE_TILES_KEY"] = old_key
    src = (ROOT / "building_3d.py").read_text(encoding="utf-8")
    assert '"photoreal": photoreal(geometry)' in src, "the model payload must carry the photoreal block"
    print("  availability rules ok")


def check_vendor() -> None:
    lib, addons = ROOT / "static/vendor/3d-tiles-renderer", ROOT / "static/vendor/three-addons"
    for f in ("LICENSE", "index.three.js", "index.three-plugins.js", "index.core-plugins.js"):
        assert (lib / f).is_file(), f
    for f in ("LICENSE", "loaders/GLTFLoader.js", "loaders/DRACOLoader.js", "utils/BufferGeometryUtils.js",
              "postprocessing/Pass.js", "libs/draco/gltf/draco_decoder.wasm", "libs/draco/gltf/draco_wasm_wrapper.js"):
        assert (addons / f).is_file(), f
    spec = re.compile(r'(?:^|[;\s}])(?:import|export)\s[^;]*?from\s*["\']([^"\']+)["\']|import\(\s*["\']([^"\']+)["\']\s*\)', re.M)
    bare = set()
    for js in list(lib.glob("*.js")) + list(addons.rglob("*.js")):
        text = js.read_text(encoding="utf-8")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)               # comments carry example URLs
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        for m in spec.finditer(text):
            s = m.group(1) or m.group(2)
            if s.startswith("."):
                assert (js.parent / s).resolve().is_file(), f"{js.name} -> {s} missing"
            else:
                bare.add(s)
    unexpected = {b for b in bare if b != "three" and not b.startswith("three/addons/")} - LAZY_ONLY
    assert not unexpected, unexpected
    for b in bare:
        if b.startswith("three/addons/"):
            assert (addons / b[len("three/addons/"):]).is_file(), b
    print("  vendored library complete ok")


def check_page() -> None:
    page = (ROOT / "templates/v2/drawing-markup.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="importmap">(.*?)</script>', page, re.S)
    assert m and '"three":"/files/static/vendor/three.module.min.js"' in m.group(1)
    assert '"three/addons/":"/files/static/vendor/three-addons/"' in m.group(1)
    assert page.index('type="importmap"') < page.index("<script>"), "the import map must come before any module load"
    assert "building3d.js?v=" in page and "onPhotorealError" in page and "prAvailable" in page
    js = (ROOT / "static/js/building3d.js").read_text(encoding="utf-8")
    assert "from '/files/static/vendor/three.module.min.js'" in js, "same URL as the import map -> one three.js instance"
    assert "azimuth: Math.PI" in js and "GoogleCloudAuthPlugin" in js and "logoUrl: pr.logo_url" in js
    assert "getAttributions" in js and "alt = 'Google Maps'" in js, "Google logo + data credits must be shown"
    print("  page + module wiring ok")


def main() -> int:
    check_availability()
    check_vendor()
    check_page()
    print("PASS smoke_photoreal_3d_353 (3 groups)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print(f"FAIL smoke_photoreal_3d_353: {e}")
        sys.exit(1)
