# Test fixtures

## 890E135_prefiled_plans_2022.pdf  (NOT in git — see below)

The primary test subject for the #293 authorable-elevations guard
(`tests/smoke_drawing_author_293.py`): the real 2022 pre-filed drawing set for
890 E 135th Street (FR-BX-001). 7 pages, AutoCAD-native VECTOR (not scanned),
2592x1728 pt per sheet, ~12.8 MB.

Sheet map (what the text-layer naming guard asserts against):

| page | sheet no. | content |
|------|-----------|---------|
| 1    | (title)   | site plan |
| 2    | A-000.00  | photo views |
| 3    | A-001.00  | ELEVATIONS — NORTH (E 135 St) |
| 4    | A-002.00  | ELEVATION — WEST (Walnut Ave) |
| 5    | A-003.00  | ELEVATIONS — SOUTH (rear yard) |
| 6    | A-004.00  | ELEVATIONS — EAST (Locust Ave) |
| 7    | A-005.00  | WEST ENTRY + typical bay elevations |

Every elevation page carries BOTH an EXISTING and a PROPOSED drawing — the
existing/proposed distinction in the sheet picker exists because of this set.

**CONTENT CAVEAT (operator, 2026-08-06):** this set specifies STO/EIFS
insulation work that was REMOVED from the actual scope. It is a
GEOMETRY / ELEVATION REFERENCE ONLY — never a scope reference. Do not derive
scope, takeoff, or materials from it.

**Why it is gitignored:** the house rule keeps large binaries and project data
out of the repo (`data_room/` contents, `*.xlsx`, DBs are all excluded); a
12.8 MB PDF committed once bloats every clone forever. The file is ignored via
`tests/fixtures/*.pdf` in `.gitignore`.

**Canonical locations** (copy from either into this directory):

- Workstation:
  `C:\Users\SSC-Admin\Documents\Claude\Projects\Superstars Dashboard\fixtures\890E135_prefiled_plans_2022.pdf`
- This repo checkout (workstation): `dashboard/tests/fixtures/` (already copied)

The guard suite also honors `SMK293_FIXTURE=<path>` and falls back to the
Documents location automatically. When the fixture is absent (fresh clone, CI),
the suite SKIPS the vector text-layer assertions loudly and still proves the
full lifecycle with a generated raster PDF — which doubles as the scanned-set
degradation test (no text layer -> blank pre-fill, everything else works).

## takeoff_sample.xls

A tiny synthetic Excel 97-2003 workbook (two sheets: a takeoff with a date and a boolean, and a totals
sheet) for `tests/smoke_doc_sheets.py`. Made once with xlwt 1.3.0 outside the app; xlwt is NOT a
dependency. No real data.
