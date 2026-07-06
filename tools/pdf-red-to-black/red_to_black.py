#!/usr/bin/env python3
"""
red_to_black.py — Convert red markings in PDFs to black.

Purpose
-------
The ASEP "Μητρώο Θεμάτων Γνώσεων" (https://info.asep.gr/mitroo-thematon-gnoseon)
publishes 11 question-bank PDFs. In each question the correct answer is printed
in red. This tool recolors every red element (text, lines, fills) to black so
the correct answer is no longer distinguishable — letting you practice the
questions blind.

What it does
------------
1. (Optional) Downloads the ASEP page and grabs every linked PDF.
2. For each PDF, rewrites the page content streams: any color-setting operator
   that selects a red-ish color (RGB, CMYK, or 3-component scn) is replaced with
   black. This recolors red text AND red vector graphics in one pass while
   leaving all other content byte-for-byte intact.
3. Reports any residual red text spans it could not reach (rare: reds defined
   via named colorspaces/patterns), so nothing silently slips through.

Usage
-----
  # Process local PDFs (already downloaded):
  python3 red_to_black.py --in ./pdfs --out ./pdfs_black

  # Process single files:
  python3 red_to_black.py a.pdf b.pdf --out ./out

  # Download the 11 ASEP PDFs then process them (needs internet access to
  # info.asep.gr — not available from every sandbox):
  python3 red_to_black.py --fetch --url https://info.asep.gr/mitroo-thematon-gnoseon --out ./asep_black

Requires: Python 3.8+, PyMuPDF (`pip install pymupdf`).
"""

import argparse
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF is required. Install it with:  pip install pymupdf")


# --------------------------------------------------------------------------- #
# Red -> black content-stream recoloring
# --------------------------------------------------------------------------- #
_NUM = r"(-?\d*\.?\d+)"
_WS = r"[\s]+"
_RGB_RE = re.compile(_NUM + _WS + _NUM + _WS + _NUM + _WS + r"(rg|RG)")
_CMYK_RE = re.compile(_NUM + _WS + _NUM + _WS + _NUM + _WS + _NUM + _WS + r"(k|K)")
_SCN_RE = re.compile(_NUM + _WS + _NUM + _WS + _NUM + _WS + r"(scn|sc|SCN|SC)")


def _is_red_rgb(r, g, b):
    return r >= 0.5 and g <= 0.45 and b <= 0.45


def _is_red_cmyk(c, m, y, k):
    # Red in CMYK is roughly (0, 1, 1, 0): strong magenta+yellow, weak cyan+black.
    return m >= 0.5 and y >= 0.5 and c <= 0.4 and k <= 0.4


def _repl_rgb(m):
    r, g, b = float(m.group(1)), float(m.group(2)), float(m.group(3))
    return f"0 0 0 {m.group(4)}" if _is_red_rgb(r, g, b) else m.group(0)


def _repl_cmyk(m):
    c, mm, y, k = (float(m.group(i)) for i in range(1, 5))
    return f"0 0 0 1 {m.group(5)}" if _is_red_cmyk(c, mm, y, k) else m.group(0)


def _repl_scn(m):
    r, g, b = float(m.group(1)), float(m.group(2)), float(m.group(3))
    return f"0 0 0 {m.group(4)}" if _is_red_rgb(r, g, b) else m.group(0)


def recolor_stream(data: bytes) -> bytes:
    """Rewrite red color operators to black. latin-1 round-trips bytes losslessly,
    so any binary regions (e.g. inline images) are preserved unchanged."""
    s = data.decode("latin-1")
    s = _RGB_RE.sub(_repl_rgb, s)
    s = _CMYK_RE.sub(_repl_cmyk, s)
    s = _SCN_RE.sub(_repl_scn, s)
    return s.encode("latin-1")


def _residual_red_text(doc):
    """Return a list of text snippets still rendered in a red-ish color."""
    reds = []
    for page in doc:
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    col = span.get("color", 0)
                    r, g, b = (col >> 16) & 255, (col >> 8) & 255, col & 255
                    if r > 128 and g < 120 and b < 120:
                        txt = span.get("text") or "".join(
                            c.get("c", "") for c in span.get("chars", [])
                        )
                        if txt.strip():
                            reds.append(txt.strip())
    return reds


def process_pdf(inp: str, outp: str) -> dict:
    doc = fitz.open(inp)
    changed = 0
    for page in doc:
        for xref in page.get_contents():
            raw = doc.xref_stream(xref)
            new = recolor_stream(raw)
            if new != raw:
                doc.update_stream(xref, new)
                changed += 1
    residual = _residual_red_text(doc)
    doc.save(outp, garbage=4, deflate=True)
    doc.close()
    return {"streams_changed": changed, "residual_red": residual}


# --------------------------------------------------------------------------- #
# Optional: fetch PDFs from the ASEP page
# --------------------------------------------------------------------------- #
class _PdfLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v and ".pdf" in v.lower():
                    self.links.append(v)


def _http_get(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (red_to_black)"})
    with urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_pdfs(page_url: str, dest: str) -> list:
    os.makedirs(dest, exist_ok=True)
    html = _http_get(page_url).decode("utf-8", "replace")
    parser = _PdfLinkParser()
    parser.feed(html)
    urls, seen = [], set()
    for href in parser.links:
        full = urljoin(page_url, href)
        if full not in seen:
            seen.add(full)
            urls.append(full)
    if not urls:
        print("No PDF links found on the page.", file=sys.stderr)
    saved = []
    for i, u in enumerate(urls, 1):
        name = os.path.basename(u.split("?")[0]) or f"section_{i}.pdf"
        path = os.path.join(dest, name)
        print(f"  [{i}/{len(urls)}] downloading {name}")
        with open(path, "wb") as f:
            f.write(_http_get(u))
        saved.append(path)
    return saved


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Convert red markings in PDFs to black.")
    ap.add_argument("files", nargs="*", help="PDF files to process")
    ap.add_argument("--in", dest="indir", help="directory of input PDFs")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--fetch", action="store_true", help="download PDFs from --url first")
    ap.add_argument(
        "--url",
        default="https://info.asep.gr/mitroo-thematon-gnoseon",
        help="page to scrape PDFs from (with --fetch)",
    )
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    inputs = list(args.files)

    if args.fetch:
        print(f"Fetching PDFs from {args.url} ...")
        tmp = os.path.join(args.out, "_downloaded")
        inputs += fetch_pdfs(args.url, tmp)

    if args.indir:
        for n in sorted(os.listdir(args.indir)):
            if n.lower().endswith(".pdf"):
                inputs.append(os.path.join(args.indir, n))

    if not inputs:
        ap.error("No input PDFs. Pass files, --in DIR, or --fetch.")

    print(f"\nProcessing {len(inputs)} PDF(s) -> {args.out}\n")
    total_residual = 0
    for path in inputs:
        name = os.path.basename(path)
        out_path = os.path.join(args.out, name)
        try:
            info = process_pdf(path, out_path)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {name}: {e}")
            continue
        msg = f"  ✓ {name}: {info['streams_changed']} stream(s) recolored"
        if info["residual_red"]:
            total_residual += len(info["residual_red"])
            sample = "; ".join(info["residual_red"][:3])
            msg += f"  ⚠ {len(info['residual_red'])} red text span(s) not reached (e.g. {sample})"
        print(msg)

    print("\nDone.")
    if total_residual:
        print(
            f"Note: {total_residual} red text span(s) used a color model this tool "
            "cannot rewrite numerically. Open those files to check manually."
        )


if __name__ == "__main__":
    main()
