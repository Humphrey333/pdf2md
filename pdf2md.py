#!/usr/bin/env python3
"""Convert every PDF under a folder tree to Markdown, mirroring the tree in an output folder."""

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber
from markitdown import DocumentConverterResult, MarkItDown, StreamInfo
from markitdown.converters import PdfConverter

# pdfplumber's default x_tolerance=3 is wider than the word gaps in tightly set book
# fonts, so words get glued together ("ThePayrollCaseStudy"). 1.5 fixed this on every
# test book while keeping table rows and code lines intact.
PDF_X_TOLERANCE = 1.5

# Glyphs whose font has no Unicode mapping come out as "(cid:N)". The ones seen so far are
# decorative, e.g. (cid:127) is the bullet between chapter and title in Martin's page headers.
CID_REPLACEMENTS = {"(cid:127)": "•"}


class BookPdfConverter(PdfConverter):
    """Plain per-page text extraction for book-style PDFs.

    markitdown's PdfConverter switches the whole document to "form" extraction if any
    single page looks column-aligned, which turns code listings into fake Markdown
    tables. Books are prose + code, so skip that detection entirely.
    """

    def convert(self, file_stream, stream_info: StreamInfo, **kwargs) -> DocumentConverterResult:
        chunks = []
        with pdfplumber.open(file_stream) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                text = _page_text_with_figures(page, page_no)
                for cid, char in CID_REPLACEMENTS.items():
                    text = text.replace(cid, char)
                if text.strip():
                    chunks.append(text.strip())
                page.close()  # pdfplumber caches per-page objects; free them as we go
        if not chunks:
            return DocumentConverterResult(markdown="")
        # The PDF path is written once; markers below only carry page + box to save tokens.
        header = (
            f"> Source PDF: {stream_info.local_path}\n"
            "> [FIG ...] markers: p.N = PDF page index (1-based, not the printed page number); "
            "box x0,top,x1,bottom = figure region in PDF points, origin top-left.\n"
        )
        return DocumentConverterResult(markdown=header + "\n" + "\n\n".join(chunks))


# --- Figure pointers --------------------------------------------------------------------
# Figures are not converted; instead a short [FIG ...] marker points at the exact PDF region
# so a reader can render just that crop when the diagram matters.

# "Figure 4-1 Coupled Payroll", "Figure 1.2: Queue", "Figure 1 .2 Classification" (OCR spacing) are
# captions; "Figure 4-2 shows the structure" is prose and must not match.
CAPTION_RE = re.compile(r"^\s*(?:Figure|Fig\.)\s*\d+(?:\s?[.\-–\s]\s?\d+)*\s*[:.]?\s+[A-Z]")
SCAN_AREA_RATIO = 0.8   # an image or shape covering >80% of the page is a scan/background
SCAN_TEXT_RATIO = 0.5   # an image under >50% of the page's text layer is a scanned page
MIN_FIG_SIZE = 40       # pt; smaller things are icons, bullets, logos
CLUSTER_GAP = 12        # pt; vector strokes closer than this belong to the same drawing
MIN_VECTOR_OBJECTS = 6  # a box + a few connectors; fewer is a rule line or code-block frame
CAPTION_REACH = 60      # pt; how far above/below a region its caption may sit


def _bbox(o) -> list:
    return [o["x0"], o["top"], o["x1"], o["bottom"]]


def _near(a, b, gap) -> bool:
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _figure_regions(page) -> list:
    """Bounding boxes of embedded images and vector drawings that look like figures."""
    pw, ph = float(page.width), float(page.height)
    # Each seed is [bbox, weight]; weight counts toward MIN_VECTOR_OBJECTS.
    seeds = []
    chars = page.chars
    for img in page.images:
        b = _bbox(img)
        w, h = b[2] - b[0], b[3] - b[1]
        # Page-sized images are scans (incl. blank pages with no text layer at all).
        if w < MIN_FIG_SIZE or h < MIN_FIG_SIZE or w * h > SCAN_AREA_RATIO * pw * ph:
            continue
        # Scanned books are one page image with an invisible OCR text layer on top. A real
        # raster figure has (almost) none of the page's text sitting over it.
        inside = sum(1 for c in chars if b[0] <= c["x0"] and c["x1"] <= b[2] and b[1] <= c["top"] and c["bottom"] <= b[3])
        if chars and inside > SCAN_TEXT_RATIO * len(chars):
            continue
        seeds.append([b, MIN_VECTOR_OBJECTS])  # a real image qualifies on its own
    for o in page.rects + page.lines + page.curves:
        b = _bbox(o)
        w, h = b[2] - b[0], b[3] - b[1]
        # Long thin strokes are header/footer rules and table borders, not diagram parts.
        if (h < 2 and w > 0.5 * pw) or (w < 2 and h > 0.5 * ph) or w * h > SCAN_AREA_RATIO * pw * ph:
            continue
        seeds.append([b, 1])

    # Greedy merge: keep absorbing neighbours until no cluster touches another.
    clusters = []
    for b, n in seeds:
        b = list(b)
        merged = True
        while merged:
            merged = False
            for c in clusters:
                if _near(b, c[0], CLUSTER_GAP):
                    b = [min(b[0], c[0][0]), min(b[1], c[0][1]), max(b[2], c[0][2]), max(b[3], c[0][3])]
                    n += c[1]
                    clusters.remove(c)
                    merged = True
                    break
        clusters.append([b, n])

    # Clip to the page: images in scans are often placed slightly off the page edge.
    clusters = [[[max(b[0], 0), max(b[1], 0), min(b[2], pw), min(b[3], ph)], n] for b, n in clusters]
    return [
        b for b, n in clusters
        if n >= MIN_VECTOR_OBJECTS and b[2] - b[0] >= MIN_FIG_SIZE and b[3] - b[1] >= MIN_FIG_SIZE
    ]


def _page_text_with_figures(page, page_no: int) -> str:
    """Page text with a [FIG ...] marker at each figure's caption, or at its position if uncaptioned."""
    lines = page.extract_text_lines(x_tolerance=PDF_X_TOLERANCE, return_chars=False)
    regions = _figure_regions(page)
    tag = {}  # line index -> marker appended to that line
    before = defaultdict(list)  # line index -> markers inserted before that line

    captions = [i for i, ln in enumerate(lines) if CAPTION_RE.match(ln["text"])]
    for b in sorted(regions, key=lambda r: r[1]):
        box = ",".join(str(round(v)) for v in b)
        # The caption is the closest unclaimed "Figure N" line just above or below the region.
        near = [
            i for i in captions
            if i not in tag and (b[1] - CAPTION_REACH <= lines[i]["top"] <= b[3] + CAPTION_REACH)
        ]
        if near:
            i = min(near, key=lambda i: min(abs(lines[i]["top"] - b[3]), abs(lines[i]["bottom"] - b[1])))
            tag[i] = f"[FIG p.{page_no} box {box}]"
        else:
            pos = next((i for i, ln in enumerate(lines) if ln["top"] >= b[1]), len(lines))
            before[pos].append(f"[FIG p.{page_no} box {box}]")
    # Captions with no detectable region (e.g. figures inside a scanned page image) still
    # get a page pointer, so the reader knows which page to open.
    for i in captions:
        tag.setdefault(i, f"[FIG p.{page_no}]")

    out = []
    for i, ln in enumerate(lines):
        out.extend(before.get(i, []))
        out.append(f"{ln['text']} {tag[i]}" if i in tag else ln["text"])
    out.extend(before.get(len(lines), []))
    return "\n".join(out)


def _log(kind: str, path: Path, detail: str = "") -> None:
    print(f"{kind} {path}" + (f": {detail}" if detail else ""), file=sys.stderr)


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def find_pdfs(input_dir: Path):
    """Yield (folder, [pdf names]) for every non-hidden folder under input_dir."""
    for root, dirs, files in os.walk(input_dir, followlinks=False):
        # Pruning dirs in place stops os.walk from ever descending into hidden folders.
        # Symlinked folders are already not entered thanks to followlinks=False.
        dirs[:] = sorted(d for d in dirs if not _is_hidden(d))
        root_path = Path(root)
        pdfs = [
            f
            for f in sorted(files)
            if not _is_hidden(f)
            and f.lower().endswith(".pdf")
            # os.walk still lists symlinked *files*; the plan says don't follow those either.
            and not (root_path / f).is_symlink()
        ]
        if pdfs:
            yield root_path, pdfs


def convert_tree(input_dir: Path, output_dir: Path) -> int:
    md = MarkItDown(enable_plugins=False)
    # Priority -1 runs before the built-in PdfConverter (0), so ours handles every PDF.
    md.register_converter(BookPdfConverter(), priority=-1.0)
    failures = 0

    for folder, pdfs in find_pdfs(input_dir):
        out_folder = output_dir / folder.relative_to(input_dir)

        # "a.pdf" and "a.PDF" would both become "a.md"; neither is allowed to win.
        by_stem = defaultdict(list)
        for name in pdfs:
            by_stem[name[: -len(".pdf")]].append(name)

        for stem, names in by_stem.items():
            if len(names) > 1:
                for name in names:
                    _log("CLASH", folder / name, f"same output name {stem}.md as {names}")
                failures += len(names)
                continue

            src = folder / names[0]
            dst = out_folder / f"{stem}.md"
            if dst.exists():
                continue

            try:
                result = md.convert_local(src)
            except Exception as e:
                _log("FAILED", src, f"{type(e).__name__}: {e}")
                failures += 1
                continue

            # A scanned PDF with no text layer converts "successfully" to nothing.
            # Writing no file means a later run (e.g. with OCR) will pick it up again.
            if not result.markdown.strip():
                _log("EMPTY", src, "no text extracted (scanned PDF?)")
                failures += 1
                continue

            # Write to a temp name and rename, so an interrupted run never leaves a
            # half-written .md that the next run would treat as done and skip.
            out_folder.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            tmp.write_text(result.markdown, encoding="utf-8")
            os.replace(tmp, dst)

    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert all PDFs under a folder (recursively) to Markdown."
    )
    parser.add_argument("-i", "--input", required=True, help="folder to scan")
    parser.add_argument("-o", "--output", required=True, help="folder to write .md files to")
    args = parser.parse_args()

    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    if not input_dir.is_dir():
        parser.error(f"input folder does not exist: {input_dir}")  # exits with code 2
    output_dir.mkdir(parents=True, exist_ok=True)

    sys.exit(convert_tree(input_dir, output_dir))


if __name__ == "__main__":
    main()
