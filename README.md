# pdf2md

Converts every PDF under a folder (including subfolders) to Markdown using
[markitdown](https://github.com/microsoft/markitdown), writing the results to an
output folder with the same folder structure.

```
in/report.pdf          ->  out/report.md
in/2024/q1/inv.PDF     ->  out/2024/q1/inv.md
```

## How it works

- Scans the input recursively for `.pdf` files (extension is case-insensitive).
- Skips hidden files/folders (names starting with `.`) and does not follow symlinks.
- If the `.md` already exists in the output folder, the PDF is skipped, so re-running
  only converts new PDFs and retries previous failures.
- Output is written via a temp file + rename, so an interrupted run never leaves a
  partial `.md` behind.
- No OCR: scanned PDFs without a text layer produce no output and are reported as `EMPTY`.
- Text is extracted per page with pdfplumber (`x_tolerance=1.5`), replacing markitdown's
  built-in PDF converter, which glued words together and turned code into fake tables in books.

## Figure markers

Figures are not converted. Each `.md` starts with the source PDF path, and each figure gets
a marker at its caption (or position) pointing to the exact region in that PDF:

```
Figure 1.4 Relationships between models and code [FIG p.26 box 145,272,441,393]
```

`p.26` is the PDF page index (1-based, not the printed page number); `box` is
`x0,top,x1,bottom` in PDF points from the top-left corner. Markers without `box` come from
a caption on a scanned page, where only the page is known. To view just the figure at 150 dpi
(points × 150/72 = pixels):

```sh
pdftoppm -png -r 150 -f 26 -l 26 -x 302 -y 567 -W 617 -H 252 book.pdf fig
```

## Install

Requires Python 3.10–3.14.

```sh
cd pdf2md
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```sh
.venv/bin/python pdf2md.py -i <input_dir> -o <output_dir>
```

The output folder is created if it does not exist.

## Log and exit codes

Only problems are printed, to stderr:

| Line | Meaning |
|---|---|
| `FAILED <path>: <error>` | markitdown could not convert the file (e.g. corrupt PDF) |
| `EMPTY <path>` | Converted, but no text was extracted (likely a scanned PDF) |
| `CLASH <path>` | Two PDFs in one folder map to the same `.md` (e.g. `a.pdf` + `a.PDF`); both are skipped |

Exit code: `0` all good, `1` at least one FAILED/EMPTY/CLASH, `2` bad arguments
(e.g. input folder missing).
