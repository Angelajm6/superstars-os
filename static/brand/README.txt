Superstars Contracting — branding asset slot (#265). SINGLE SWAP POINT.

Replace these files to rebrand the WHOLE app + all reports with NO code change:
  mark.svg        — the company mark/logo (used everywhere; the star-only mark).
  logo-light.svg  — variant for LIGHT backgrounds (defaults to the mark).
  logo-dark.svg   — variant for DARK backgrounds (defaults to the mark).

Requirements for a swapped logo (so it renders legibly + never distorts):
  - SVG preferred (clean scaling); PNG with a TRANSPARENT background also OK.
  - Transparent background (no opaque rectangle).
  - Any aspect ratio is fine — it scales to fit a reserved, fixed-height box via
    preserveAspectRatio (SVG) / object-fit:contain (raster). It will NOT stretch.
  - For a logo that must differ on light vs dark, supply logo-light/logo-dark.

Brand COLOURS are theme tokens, not in these files — see brand.py (BRAND) and the
:root tokens in static/css/widgets.css. Update those for a colourway change.
The company NAME/wordmark text is brand.py COMPANY_NAME.

logo-full.png — the full printed logo (star + SUPERSTARS / CONTRACTING INC), exactly as
  supplied. It is the top-left brand in every app sidebar/top bar (.brand-full, widgets.css);
  the star (mark.svg) stays for the collapsed icon rail, favicons and reports.
