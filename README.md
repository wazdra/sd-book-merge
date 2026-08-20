# sd-book-merge

ScienceDirect serves books one chapter at a time. `sdbookmerge` merges the
chapters back into one PDF, in the right order, keeping the original printed
page numbers.

```sh
sdbookmerge PATH/TO/FOLDER
# -> PATH/TO/{Title}.pdf
```

Point it at the folder of chapters or at the `.zip` ScienceDirect gave you; the
archive is unpacked to a temporary directory and handled the same way.

## Features

- **Automatic reading order**, taken from the chapter DOI that Elsevier stores in each PDF's `/Title` field. 
- **Correct page numbering.** The merged file gets a rebuilt `/PageLabels` tree, so typing `150` in your viewer lands on the page with `150` printed on it. 
- **DOI check.** DOI order is checked against the printed folios in each PDF's `/PageLabels`; they must come out strictly increasing and non-overlapping. On a conflict the script reports it and refuses to write, rather than hand you a scrambled book.
- **Real chapter titles** in the bookmarks, resolved from CrossRef.
- **Missing pages reported.** Downloads often omit a folio or two (a contents page, a blank verso). Those gaps are listed; `--fill-gaps` inserts blank placeholders so physical position matches printed number exactly.
- **Lossless.** Chapters are copied verbatim, never re-encoded; the OCR layer and quality are untouched. Shared objects are deduplicated.

## Dependencies

Python 3.9+ and [`pypdf`](https://pypi.org/project/pypdf/). Nothing else.


```sh
pip install pypdf
```

Or run it with [uv](https://docs.astral.sh/uv/) and skip the install; the script
declares its dependency inline (PEP 723):

```sh
uv run sdbookmerge.py DIRECTORY
```

To put it on your `PATH`:

```sh
chmod +x sdbookmerge.py && sudo cp sdbookmerge.py /usr/local/bin/sdbookmerge
```

Tested on macOS. Pure Python with no shell-outs, so Linux, WSL and native
Windows should work, though it was not tested.

## Usage

```
sdbookmerge [PATH] [options]
```

`PATH` is the folder of chapter PDFs or the ScienceDirect `.zip`, and defaults to
the current directory. Output goes *beside* the input, so re-running never eats
its own output.

```sh
sdbookmerge PATH/TO/BOOK.zip
```

Inside a zip the PDFs are found whether they sit at the top level or in a
wrapping folder; nothing but PDFs is extracted, and entries naming a path
outside the archive are refused.

| Option | Effect |
| --- | --- |
| `-o, --output FILE` | Output path. Default: `<Book Title>.pdf` next to the input. |
| `--dry-run` | Print the plan and verification result; write nothing. |
| `--offline` | Skip CrossRef; take chapter titles from filenames. |
| `--mailto EMAIL` | Your address, for CrossRef's faster "polite pool". |
| `--order doi\|folio\|name` | Force an ordering strategy instead of `auto`. |
| `--fill-gaps` | Insert blank pages where printed page numbers are missing. |
| `--title`, `--author` | Override document metadata. |
| `--force` | Write the file even though verification failed. |
| `--timeout SECONDS` | CrossRef request timeout (default 20). |

Exit codes: `0` success, `1` bad invocation, `2` verification failed.

**Network use.** By default, one HTTPS GET per chapter to `api.crossref.org`, a
free public metadata API — no key, no account. It sends only the DOI already
embedded in your PDF. `--offline` disables it; ordering and page numbering don't
depend on it, only the bookmark titles get worse.

## Disclaimer -- AI use

This project was mainly built with AI. It comes with no warranty whatsoever.

## License

MIT — see [LICENSE](LICENSE).
