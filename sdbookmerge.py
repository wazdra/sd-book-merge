#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pypdf>=4.2"]
# ///
"""
sdbookmerge - reassemble a ScienceDirect book from its per-chapter PDFs.

ScienceDirect serves books one chapter at a time. This script puts them back
together in the right order, with the original printed page numbers preserved
as PDF page labels and a bookmark per chapter.

It never guesses from filenames if it can avoid it. Ordering and pagination
come from two independent sources that are checked against each other:

  1. The chapter DOI, stored by Elsevier in each PDF's /Title metadata
     (e.g. "doi:10.1016/S0079-8169(08)60529-2"). Elsevier assigns these in
     book order, so sorting by DOI reproduces the reading order.

  2. The /PageLabels of each chapter PDF, which carry the real printed
     folios ("vii", "99", ...). These must form a strictly increasing,
     non-overlapping sequence once the chapters are in order.

If the two disagree, that is a bug in the assembly and the script stops
rather than emit a scrambled book (override with --force or --order).

Optionally (on by default, disable with --offline) each DOI is resolved
against the public CrossRef API to recover exact chapter titles, the book
title, and a third independent copy of the page ranges.

The input is either the folder of chapter PDFs or, just as well, the .zip
ScienceDirect hands you: the archive is unpacked to a temporary directory and
treated identically.

Usage:
    python3 sdbookmerge.py /path/to/ScienceDirect_articles_folder
    python3 sdbookmerge.py ~/Downloads/ScienceDirect_articles_20Aug2026.zip
    python3 sdbookmerge.py . --dry-run
    python3 sdbookmerge.py . -o "Isaacs - Character Theory.pdf"

Requires pypdf.  `pip install pypdf`, or run the whole thing with
`uv run sdbookmerge.py ...` and let uv handle it.

Copyright (c) 2026 Anatole Dahan.  MIT licensed; see LICENSE.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover
    sys.exit(
        "sdbookmerge needs pypdf.\n"
        "  pip install pypdf\n"
        "or run this script with:  uv run sdbookmerge.py ..."
    )

VERSION = "1.0.0"
CROSSREF_API = "https://api.crossref.org/works/"

# PDF page label styles (PDF 32000-1:2008, table 159).
DECIMAL, ROMAN_UPPER, ROMAN_LOWER, ALPHA_UPPER, ALPHA_LOWER = "/D", "/R", "/r", "/A", "/a"

# Books number their front matter in roman and their body in arabic, so the
# style tells us which block a folio belongs to before its value matters.
STYLE_RANK = {ROMAN_LOWER: 0, ROMAN_UPPER: 0, DECIMAL: 1, ALPHA_LOWER: 2, ALPHA_UPPER: 2}


# ---------------------------------------------------------------------------
# Page label vocabulary
# ---------------------------------------------------------------------------

ROMAN_VALUES = [
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
    (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
    (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
]


def int_to_roman(n: int) -> str:
    if n <= 0:
        return str(n)
    out = []
    for value, numeral in ROMAN_VALUES:
        count, n = divmod(n, value)
        out.append(numeral * count)
    return "".join(out)


def roman_to_int(s: str) -> Optional[int]:
    digits = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    if not s or any(ch not in digits for ch in s):
        return None
    total, previous = 0, 0
    for ch in reversed(s):
        value = digits[ch]
        total += value if value >= previous else -value
        previous = max(previous, value)
    return total if int_to_roman(total) == s else None


def int_to_alpha(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA, 53 -> AAA (PDF spec, not spreadsheet-style)."""
    if n <= 0:
        return str(n)
    index, repeat = (n - 1) % 26, (n - 1) // 26 + 1
    return chr(ord("a") + index) * repeat


@dataclass(frozen=True)
class Label:
    """One printed folio: a numbering style, an optional prefix, a value."""

    style: Optional[str]
    prefix: str
    number: Optional[int]

    def __str__(self) -> str:
        if self.style is None or self.number is None:
            return self.prefix
        if self.style == DECIMAL:
            body = str(self.number)
        elif self.style == ROMAN_LOWER:
            body = int_to_roman(self.number)
        elif self.style == ROMAN_UPPER:
            body = int_to_roman(self.number).upper()
        elif self.style == ALPHA_LOWER:
            body = int_to_alpha(self.number)
        elif self.style == ALPHA_UPPER:
            body = int_to_alpha(self.number).upper()
        else:
            body = str(self.number)
        return self.prefix + body

    @property
    def block(self) -> tuple:
        """Folios are only comparable inside the same numbering block."""
        return (STYLE_RANK.get(self.style, 3), self.prefix)

    def sort_key(self) -> tuple:
        return (self.block, self.number if self.number is not None else 0)

    def shifted(self, delta: int) -> "Label":
        return Label(self.style, self.prefix, (self.number or 0) + delta)


def parse_folio(text: str) -> Optional[Label]:
    """Parse a printed folio as it appears in a CrossRef `page` field."""
    text = text.strip()
    if not text:
        return None
    match = re.fullmatch(r"([^0-9ivxlcdmIVXLCDM]*)([0-9]+|[ivxlcdm]+|[IVXLCDM]+)", text)
    if not match:
        return None
    prefix, body = match.groups()
    if body.isdigit():
        return Label(DECIMAL, prefix, int(body))
    value = roman_to_int(body)
    if value is None:
        return None
    return Label(ROMAN_UPPER if body.isupper() else ROMAN_LOWER, prefix, value)


# ---------------------------------------------------------------------------
# Reading what Elsevier put in the files
# ---------------------------------------------------------------------------

def read_page_labels(reader: PdfReader, page_count: int) -> Optional[list]:
    """Extract the printed folio of every page from the PDF's /PageLabels tree.

    Returns None when the file carries no page labels at all.
    """
    root = reader.trailer["/Root"]
    if "/PageLabels" not in root:
        return None

    ranges: list = []

    def walk(node) -> None:
        node = node.get_object()
        nums = node.get("/Nums")
        if nums is not None:
            nums = nums.get_object()
            for i in range(0, len(nums) - 1, 2):
                ranges.append((int(nums[i].get_object()), nums[i + 1].get_object()))
        for kid in node.get("/Kids", []) or []:
            walk(kid)

    walk(root["/PageLabels"])
    if not ranges:
        return None
    ranges.sort(key=lambda item: item[0])

    labels = []
    for index in range(page_count):
        applicable = None
        for start, spec in ranges:
            if start <= index:
                applicable = (start, spec)
            else:
                break
        if applicable is None:
            labels.append(Label(DECIMAL, "", index + 1))
            continue
        start, spec = applicable
        style = spec.get("/S")
        style = str(style) if style is not None else None
        prefix = str(spec.get("/P", ""))
        first = int(spec.get("/St", 1))
        number = None if style is None else first + (index - start)
        labels.append(Label(style, prefix, number))
    return labels


def read_doi(reader: PdfReader) -> Optional[str]:
    """Elsevier stores the chapter DOI in the /Title metadata field."""
    title = str((reader.metadata or {}).get("/Title", "") or "")
    match = re.search(r"\b10\.\d{4,9}/\S+", title)
    return match.group(0).rstrip(".,;") if match else None


def title_from_filename(path: Path) -> str:
    """Fallback only. ScienceDirect flattens punctuation into dashes, so this
    cannot round-trip exactly: "Brauer's theorem" arrives as "Brauer-s-theorem".
    """
    stem = path.stem
    stem = re.sub(r"_\d{4}_.*$", "", stem)  # drop the _<year>_<series> tail
    return re.sub(r"\s+", " ", stem.replace("-", " ")).strip() or path.stem


def natural_key(text: str) -> list:
    """Sort DOIs so that ...60529 < ...60530 and .00003 < .00010."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


# ---------------------------------------------------------------------------
# CrossRef
# ---------------------------------------------------------------------------

def crossref_lookup(doi: str, mailto: Optional[str], timeout: float) -> Optional[dict]:
    url = CROSSREF_API + urllib.parse.quote(doi, safe="/()")
    if mailto:
        url += "?" + urllib.parse.urlencode({"mailto": mailto})
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": f"sdbookmerge/{VERSION}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response).get("message")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def book_title_from(container_titles: list) -> Optional[str]:
    """CrossRef gives ['<series>', '<book>'] for books published in a series."""
    titles = [t for t in (container_titles or []) if t]
    return titles[-1] if titles else None


# ---------------------------------------------------------------------------
# Finding the chapter PDFs
# ---------------------------------------------------------------------------

def is_chapter_pdf(path: Path) -> bool:
    return (
        path.suffix.lower() == ".pdf"
        and not path.name.startswith("._")  # macOS resource forks
        and "__MACOSX" not in path.parts
        and path.is_file()
    )


def collect_pdfs(root: Path) -> list:
    """The chapter PDFs under `root`: top level if there are any, else deeper.

    An unpacked download is flat, but a zip may wrap everything in a folder,
    and browsers sometimes add another one on top of that.
    """
    top = sorted(p for p in root.iterdir() if is_chapter_pdf(p))
    return top or sorted(p for p in root.rglob("*") if is_chapter_pdf(p))


def extract_pdfs(archive: Path, into: Path) -> int:
    """Unpack the PDFs of a ScienceDirect download into `into`.

    Only PDFs are taken, and only those whose stored path stays inside the
    destination: a zip is untrusted input and may name `../` or `/etc/`.
    """
    destination = into.resolve()
    count = 0
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if entry.is_dir():
                continue
            name = Path(entry.filename)
            if not (name.suffix.lower() == ".pdf" and not name.name.startswith("._")):
                continue
            if "__MACOSX" in name.parts:
                continue
            target = (destination / name).resolve()
            if not target.is_relative_to(destination):
                print(f"  ! skipping entry outside the archive: {entry.filename}", file=sys.stderr)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------

@dataclass
class Part:
    path: Path
    reader: PdfReader
    page_count: int
    doi: Optional[str]
    labels: Optional[list]
    title: str
    title_source: str
    label_source: str
    crossref: Optional[dict] = field(default=None, repr=False)

    @property
    def first(self) -> Optional[Label]:
        return self.labels[0] if self.labels else None

    @property
    def last(self) -> Optional[Label]:
        return self.labels[-1] if self.labels else None

    def folio_range(self) -> str:
        if not self.labels:
            return "?"
        first, last = str(self.first), str(self.last)
        return first if first == last else f"{first}-{last}"


def load_parts(paths: list) -> list:
    parts = []
    for path in sorted(paths):
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                print(f"  ! skipping encrypted file: {path.name}", file=sys.stderr)
                continue
        page_count = len(reader.pages)
        labels = read_page_labels(reader, page_count)
        parts.append(
            Part(
                path=path,
                reader=reader,
                page_count=page_count,
                doi=read_doi(reader),
                labels=labels,
                title=title_from_filename(path),
                title_source="filename",
                label_source="pdf" if labels else "none",
            )
        )
    return parts


def close_parts(parts: list) -> None:
    """Release the source handles; a temporary directory holding an open file
    cannot be removed on Windows."""
    for part in parts:
        try:
            part.reader.close()
        except Exception:
            pass


def enrich_from_crossref(parts: list, mailto: Optional[str], timeout: float) -> dict:
    """Fill in exact titles, and page labels for parts that lack them."""
    resolved = 0
    with_doi = [p for p in parts if p.doi]
    progress = sys.stderr.isatty()
    for index, part in enumerate(with_doi, 1):
        if progress:
            print(f"\r  querying CrossRef {index}/{len(with_doi)}...", end="", file=sys.stderr, flush=True)
        message = crossref_lookup(part.doi, mailto, timeout)
        if not message:
            continue
        resolved += 1
        part.crossref = message
        titles = [t for t in (message.get("title") or []) if t]
        if titles:
            part.title = re.sub(r"\s+", " ", titles[0]).strip()
            part.title_source = "crossref"
        if part.labels is None:
            start = parse_folio((message.get("page") or "").split("-")[0])
            if start is not None:
                part.labels = [start.shifted(i) for i in range(part.page_count)]
                part.label_source = "crossref"
    if progress:
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr, flush=True)

    book = {}
    for part in parts:
        if not part.crossref:
            continue
        book.setdefault("title", book_title_from(part.crossref.get("container-title")))
        book.setdefault("publisher", part.crossref.get("publisher"))
        issued = (part.crossref.get("published-print") or part.crossref.get("issued") or {})
        date_parts = (issued.get("date-parts") or [[None]])[0]
        book.setdefault("year", date_parts[0] if date_parts else None)
        series = [t for t in (part.crossref.get("container-title") or []) if t]
        book.setdefault("series", series[0] if len(series) > 1 else None)
    book["resolved"] = resolved
    return book


def fill_missing_labels(parts: list) -> None:
    """Assume decimal numbering from 1 for any part still without labels.

    Only correct for a part that really does start on printed page 1, so it is
    reported and then checked by the continuity pass like everything else.
    """
    for part in parts:
        if part.labels is None:
            part.labels = [Label(DECIMAL, "", i + 1) for i in range(part.page_count)]
            part.label_source = "assumed"


# ---------------------------------------------------------------------------
# Ordering and verification
# ---------------------------------------------------------------------------

def order_parts(parts: list, strategy: str) -> tuple:
    """Return (ordered_parts, strategy_used)."""
    if strategy == "auto":
        strategy = "doi" if all(p.doi for p in parts) else (
            "folio" if all(p.labels for p in parts) else "name"
        )
    if strategy == "doi":
        if not all(p.doi for p in parts):
            missing = [p.path.name for p in parts if not p.doi][:3]
            raise SystemExit(f"--order doi needs a DOI in every file; missing in {missing}")
        return sorted(parts, key=lambda p: natural_key(p.doi)), "doi"
    if strategy == "folio":
        if not all(p.labels for p in parts):
            raise SystemExit("--order folio needs page labels in every file")
        return sorted(parts, key=lambda p: p.first.sort_key()), "folio"
    return sorted(parts, key=lambda p: natural_key(p.path.name)), "name"


def verify(ordered: list) -> tuple:
    """Check the assembled sequence. Returns (problems, gaps)."""
    problems, gaps = [], []
    previous = None

    for part in ordered:
        labels = part.labels or []
        for index in range(1, len(labels)):
            before, after = labels[index - 1], labels[index]
            if before.block != after.block or (after.number or 0) != (before.number or 0) + 1:
                problems.append(
                    f"{part.path.name}: page labels jump {before} -> {after} inside the file"
                )
                break

        if previous is None or not labels:
            previous = part
            continue

        last, first = previous.last, part.first
        if last.block == first.block:
            step = (first.number or 0) - (last.number or 0)
            if step <= 0:
                problems.append(
                    f"overlap or backwards step: {previous.path.name} ends at {last}, "
                    f"{part.path.name} starts at {first}"
                )
            elif step > 1:
                missing = [last.shifted(i) for i in range(1, step)]
                gaps.append((previous, part, missing))
        elif first.block < last.block:
            problems.append(
                f"numbering block goes backwards: {previous.path.name} ends at {last} "
                f"({last.style}), {part.path.name} starts at {first} ({first.style})"
            )
        previous = part

    return problems, gaps


def cross_check_crossref(ordered: list) -> list:
    """Compare our page labels with the page range CrossRef reports."""
    mismatches = []
    for part in ordered:
        if not part.crossref or not part.labels or part.label_source == "crossref":
            continue
        reported = (part.crossref.get("page") or "").strip()
        if not reported:
            continue
        if reported.replace(" ", "") != part.folio_range().replace(" ", ""):
            mismatches.append(f"{part.path.name}: PDF says {part.folio_range()}, CrossRef says {reported}")
    return mismatches


def check_single_book(ordered: list) -> list:
    titles = {
        book_title_from(p.crossref.get("container-title"))
        for p in ordered
        if p.crossref
    }
    titles.discard(None)
    return sorted(titles)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def compress_label_runs(labels: list) -> list:
    """Collapse per-page labels into the minimal set of /PageLabels ranges."""
    runs = []
    for index, label in enumerate(labels):
        if runs:
            start, previous = runs[-1]
            expected = labels[start].shifted(index - start)
            if label == expected:
                continue
        runs.append((index, label))
    return [(start, labels[start]) for start, _ in runs]


def build(ordered: list, output: Path, book: dict, fill_gaps: bool, gaps: list) -> dict:
    writer = PdfWriter()
    labels: list = []
    blanks_added = 0

    gap_before = {id(after): missing for _, after, missing in gaps}

    for part in ordered:
        if fill_gaps:
            for missing in gap_before.get(id(part), []):
                template = part.reader.pages[0]
                box = template.mediabox
                writer.add_blank_page(width=box.width, height=box.height)
                labels.append(missing)
                blanks_added += 1

        start_index = len(writer.pages)
        writer.append(part.reader, import_outline=False)
        labels.extend(part.labels)
        writer.add_outline_item(part.title, start_index)

    # Drop anything inherited from the sources, then write our own labels.
    if "/PageLabels" in writer._root_object:
        del writer._root_object["/PageLabels"]
    runs = compress_label_runs(labels)
    for run_index, (start, label) in enumerate(runs):
        end = runs[run_index + 1][0] - 1 if run_index + 1 < len(runs) else len(labels) - 1
        writer.set_page_label(
            start, end,
            style=label.style or DECIMAL,
            prefix=label.prefix or None,
            start=label.number or 1,
        )

    writer.add_metadata(
        {
            key: value
            for key, value in {
                "/Title": book.get("title"),
                "/Author": book.get("author"),
                "/Subject": book.get("series"),
                "/Producer": f"sdbookmerge {VERSION}",
                "/Creator": f"sdbookmerge {VERSION}",
            }.items()
            if value
        }
    )
    writer.page_mode = "/UseOutlines"

    if hasattr(writer, "compress_identical_objects"):
        writer.compress_identical_objects()

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as handle:
        writer.write(handle)

    return {"pages": len(writer.pages), "blanks": blanks_added}


def safe_filename(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", text)
    return re.sub(r"\s+", " ", text).strip(" .") or "merged-book"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="sdbookmerge",
        description="Merge per-chapter ScienceDirect PDFs back into one book.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ordering comes from the chapter DOI in each PDF's /Title metadata and is\n"
            "verified against the printed page numbers in each PDF's /PageLabels.\n"
            "The merged file keeps the original folios as page labels, so your viewer's\n"
            "page number matches the number printed on the page."
        ),
    )
    parser.add_argument("source", nargs="?", default=".", type=Path, metavar="PATH",
                        help="folder of chapter PDFs, or the .zip ScienceDirect "
                             "gave you (default: current directory)")
    parser.add_argument("-o", "--output", type=Path,
                        help="output file (default: '<Book Title>.pdf' beside the input)")
    parser.add_argument("--order", choices=["auto", "doi", "folio", "name"], default="auto",
                        help="how to determine reading order (default: auto)")
    parser.add_argument("--offline", action="store_true",
                        help="do not contact CrossRef; use filenames for chapter titles")
    parser.add_argument("--mailto", metavar="EMAIL",
                        help="your email, for CrossRef's faster 'polite pool'")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="CrossRef request timeout in seconds (default: 20)")
    parser.add_argument("--title", help="override the book title")
    parser.add_argument("--author", help="set the book author")
    parser.add_argument("--fill-gaps", action="store_true",
                        help="insert blank pages where printed page numbers are missing")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the plan and verification result, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="write the file even if verification fails")
    parser.add_argument("--version", action="version", version=f"sdbookmerge {VERSION}")
    args = parser.parse_args(argv)

    source = args.source.expanduser().resolve()
    with ExitStack() as stack:
        if source.is_dir():
            root = source
        elif not source.exists():
            return fail(f"no such file or directory: {source}")
        elif zipfile.is_zipfile(source):
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="sdbookmerge-")))
            print(f"Unpacking {source.name}")
            if not extract_pdfs(source, root):
                return fail(f"no PDF files inside {source}")
        else:
            return fail(f"not a folder or a zip archive: {source}")
        return run(args, source, root, stack)


def run(args: argparse.Namespace, source: Path, root: Path, stack: ExitStack) -> int:
    """Merge the PDFs under `root`. `source` is what the user actually named --
    the same folder, or the zip it came out of -- and anchors the output.
    """
    output = (args.output.expanduser().resolve() if args.output else None)
    candidates = [
        p for p in collect_pdfs(root)
        if output is None or p.resolve() != output
    ]
    if not candidates:
        return fail(f"no PDF files in {source}")

    print(f"Reading {len(candidates)} PDFs from {source}")
    parts = load_parts(candidates)
    if not parts:
        return fail("no readable PDFs")
    stack.callback(close_parts, parts)

    book: dict = {}
    if args.offline:
        print("  offline mode: chapter titles come from filenames")
    else:
        book = enrich_from_crossref(parts, args.mailto, args.timeout)
        print(f"  CrossRef resolved {book.get('resolved', 0)}/{len(parts)} chapters")

    missing_labels = [p for p in parts if p.labels is None]
    if missing_labels:
        print(f"  {len(missing_labels)} file(s) carry no page labels; assuming they start at page 1:")
        for part in missing_labels:
            print(f"      {part.path.name}")
        fill_missing_labels(parts)

    ordered, strategy = order_parts(parts, args.order)
    print(f"  reading order determined by: {strategy}")

    problems, gaps = verify(ordered)
    mismatches = cross_check_crossref(ordered)
    books_seen = check_single_book(ordered)

    # --- the plan -----------------------------------------------------------
    print()
    width = max(len(p.title) for p in ordered)
    width = min(max(width, 20), 60)
    print(f"  {'pages':>9}  {'folios':>11}  {'src':<9} title")
    print(f"  {'-' * 9}  {'-' * 11}  {'-' * 9} {'-' * width}")
    position = 1
    for part in ordered:
        span = f"{position}-{position + part.page_count - 1}"
        position += part.page_count
        print(f"  {span:>9}  {part.folio_range():>11}  {part.label_source:<9} {part.title[:width]}")
    print(f"\n  {position - 1} pages total")

    if len(books_seen) > 1:
        problems.append(f"chapters come from more than one book: {books_seen}")
    if mismatches:
        problems.extend(f"page range disagrees with CrossRef -- {m}" for m in mismatches)

    if gaps:
        print("\n  Missing printed pages (not in the download):")
        for before, after, missing in gaps:
            shown = ", ".join(str(label) for label in missing[:8])
            if len(missing) > 8:
                shown += f", ... ({len(missing)} pages)"
            print(f"      {shown}   between '{before.title}' and '{after.title}'")
        if not args.fill_gaps:
            print("      page labels stay correct; pass --fill-gaps to insert blank placeholders")

    if problems:
        sys.stdout.flush()
        print("\n  VERIFICATION FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"      - {problem}", file=sys.stderr)
        if not args.force:
            print(
                "\n  Refusing to write a book that may be out of order.\n"
                "  Try --order folio, or --force to write it anyway.",
                file=sys.stderr,
            )
            return 2
        print("  --force given, continuing anyway.", file=sys.stderr)
    else:
        print("\n  Verified: chapter order and page numbering are consistent.")

    title = args.title or book.get("title") or (
        source.name if source.is_dir() else source.stem
    )
    book["title"] = title
    if args.author:
        book["author"] = args.author

    if output is None:
        output = source.parent / f"{safe_filename(title)}.pdf"

    if args.dry_run:
        print(f"\n  Dry run. Would write: {output}")
        return 0

    print(f"\n  Writing {output}")
    result = build(ordered, output, book, args.fill_gaps, gaps)
    size_mb = output.stat().st_size / 1_000_000
    note = f", {result['blanks']} blank placeholders" if result["blanks"] else ""
    print(f"  Done: {result['pages']} pages{note}, {size_mb:.1f} MB")
    return 0


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
