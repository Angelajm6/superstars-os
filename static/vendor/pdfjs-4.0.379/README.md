pdf.js 4.0.379 (Mozilla, Apache-2.0): build/pdf.min.mjs and build/pdf.worker.min.mjs from the npm package
pdfjs-dist@4.0.379, renamed .js so they are served as JavaScript. sha256 prefixes: pdf 61c2ab9c464aabd5,
worker c0408d13d3a12874 (checked against npm 2026-10-21). Used by static/js/pdf_view.js to draw proposal PDFs
as page images on every device (iPhone Safari shows only the first page of an embedded PDF; the app also
forbids framing its own PDFs).
