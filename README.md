# Pustaka Taratsa Scraper

Scraper collection to gather books from various Indonesian digital library sources for upload to [https://pustaka.taratsa.id/](https://pustaka.taratsa.id/) — an Indonesian digital library platform.

## Sources

| Source | URL | Description |
|--------|-----|-------------|
| Langka Logos | `https://langka.logosid.app/` | Paginated JSON API |
| MasasilaM | `https://masasilam.com/api/books` | Indonesian books |
| Kacabenggala | `https://kabe.drepram.com/` | Static Next.js site with PDF downloads |
| Kemendikdasmen | Repositori perpustakaan Kemendikdasmen | Educational materials |
| Archipelago AAA | Archive.org collection | Historical archives |
| Api Kartini | Google Drive dump | Indonesian women's magazine archive |

## Scripts

- `scrape_langka_logos.py` — Langka Logos API scraper
- `scrape_masasilam.py` — MasasilaM scraper
- `scrape_kabe.py` — Kacabenggala scraper
- `scrape_kemendikdasmen.py` — Kemendikdasmen scraper
- `scrape_archipelago_aaa.py` — Archipelago AAA scraper
- `scrape_kartini_drive.py` — Api Kartini Google Drive scraper
- `pdf_metadata_embed.py` — Embed PDF metadata (Info + XMP)
- `verify_pdf_metadata.py` — Verify embedded PDF metadata

## Requirements

This project uses **UV** for Python package management.

- Python: 3.14
- Install: `uv sync`
- Add package: `uv add <package>`
- Update: `uv sync`

## Output

All output goes to `output/`:

```
output/
├── books.json / books.csv              # Langka Logos
├── langka_logos/
├── masasilam/
├── kabe/
├── kemendikdasmen/
├── archipelago_aaa/
├── api_kartini/
└── repositori/
```

## Quick Start

```bash
# Run any script with UV (auto-uses venv)
uv run python scrape_langka_logos.py

# With options
uv run python scrape_langka_logos.py --download-covers --download-docs
uv run python scrape_langka_logos.py --download-docs-with-metadata

# Other scrapers
uv run python scrape_masasilam.py
uv run python scrape_kabe.py
uv run python scrape_kemendikdasmen.py
uv run python scrape_archipelago_aaa.py
uv run python scrape_kartini_drive.py
```

## Langka Logos (`langka.logosid.app`)

Scrapes `https://langka.logosid.app/api/books` (paginated JSON) and writes normalized metadata to `output/books.json` and `output/books.csv`.

Optionally downloads cover images and document files.

### Common options

```bash
# Basic scraping
python3 scrape_langka_logos.py --out-dir output
python3 scrape_langka_logos.py --start-page 1 --end-page 2

# Download covers (optional)
python3 scrape_langka_logos.py --download-covers --covers-dir output/covers
# Docs are downloaded by default (only missing files)
python3 scrape_langka_logos.py --docs-dir output/docs
python3 scrape_langka_logos.py --docs-dir output/docs --insecure-ssl
# Disable docs download pass
python3 scrape_langka_logos.py --no-download-docs

# Download docs and immediately embed metadata (Info + XMP, good for Calibre-Web)
python3 scrape_langka_logos.py --download-docs-with-metadata --docs-dir output/docs
```

### Output

- `output/books.json`: `{ "meta": { ... }, "books": [ ... ] }`
- `output/books.csv`: one row per book

With `--download-docs` you also get:

- `output/docs/<id>-<slug>.pdf` (and other formats if present)
- `output/docs/docs_manifest.json`
- `output/docs/docs_manifest.csv`

The docs manifest repeats `title`, `author`, `category` from the API and sets `tags = [category]`.

Notes:
- 0‑byte downloads are treated as failures and removed.
- `--insecure-ssl` only relaxes TLS validation; it cannot fix hosts that return empty/blocked content.

### Embedded PDF Metadata (for Calibre / Calibre-Web)

To embed metadata directly *into* already-downloaded PDFs (Info dict + optional XMP), run:

```bash
python3 scrape_langka_logos.py \
  --write-pdf-metadata \
  --pdf-metadata-manifest output/docs/docs_manifest.json \
  --pdf-metadata-calibre-web-xmp
```

This writes:
- `/Title` = book `title`
- `/Author` = book `author`
- `/Subject` = book `category`
- `/Keywords` = `tags` (i.e. category)
- XMP `dc:title`, `dc:creator`, `dc:description`, `dc:subject` (so Calibre-Web's uploader can see author/title)

Optional:
- `--pdf-metadata-backup` to create `.bak` before rewriting
- `--pdf-metadata-limit N` to test on only the first N PDFs

## MasasilaM (`masasilam.com`)

Separate script for [`https://masasilam.com/api/books`](https://masasilam.com/api/books):

```bash
# List all books → output/masasilam/books.json + books.csv
python3 scrape_masasilam.py

# Larger page size (fewer HTTP calls)
python3 scrape_masasilam.py --limit 50

# Files are downloaded by default (only missing) + manifest
python3 scrape_masasilam.py --books-dir output/masasilam/files
# Disable file download pass
python3 scrape_masasilam.py --no-download-books
```

Outputs:

- `output/masasilam/books.json` / `books.csv` — raw API list fields + `source_page`
- With `--download-books`: `output/masasilam/files/<id>-<slug>.<ext>` and `books_manifest.json` / `.csv` with `title`, `author` (`authorNames`), `category`, `tags` (category + split `genres`)

## Kacabenggala / [kabe.drepram.com](https://kabe.drepram.com/)

Static Next.js site: work list from `/` or `/works`, each `/works/<slug>` exposes schema.org `Book` JSON-LD and a PDF at `/api/documents/file/...`.

```bash
python3 scrape_kabe.py
python3 scrape_kabe.py --index /works --out-dir output/kabe
# PDFs are downloaded by default (only missing files), with metadata embedding (Info + XMP)
python3 scrape_kabe.py --files-dir output/kabe/files
# Disable PDF download pass
python3 scrape_kabe.py --no-download-pdfs
# Re-embed only (uses books_manifest.json; needs `pypdf` in `.pydeps` like Langka scraper)
python3 scrape_kabe.py --embed-pdf-metadata-only --files-dir output/kabe/files
```

Use `--no-embed-pdf-metadata` with `--download-pdfs` to skip embedding. `--embed-backup` keeps a `.bak` before rewriting.

Outputs:

- `output/kabe/books.json` / `books.csv`: `slug`, `title`, `author`, `year`, `pdf_url`, `url`, …
- With `--download-pdfs`: `output/kabe/files/<slug>.pdf` plus `books_manifest.json` / `.csv` (includes `subject`, `description`, `publisher` = **Kacabenggala** in PDF Info `/Company` and XMP `dc:publisher`)

Shared helper: `pdf_metadata_embed.py` (also used by `scrape_langka_logos.py` / `scrape_books.py` for PDF metadata).

## Api Kartini Google Drive dump

Use this when you want all `/file/d/<id>/edit` documents linked from Api Kartini:

```bash
# Download all files listed in output/api_kartini.json and embed PDF metadata (Info + XMP)
python3 scrape_kartini_drive.py --source-json output/api_kartini.json --out-dir output/api_kartini_files

# If files already exist, only (re)embed metadata from manifest
python3 scrape_kartini_drive.py --embed-metadata-only --out-dir output/api_kartini_files
# Disable download pass explicitly
python3 scrape_kartini_drive.py --no-download-files --out-dir output/api_kartini_files
```

Notes:
- Default embedded metadata: `author = "Majalah Api Kartini"`, `subject = "Api Kartini"`, `publisher = "Jajasan Melati"`.
- XMP is enabled by default to improve Calibre-Web ingestion reliability.
- Disable metadata embedding with `--no-embed-metadata`, or disable XMP with `--no-xmp`.

### Verify embedded metadata (pre Calibre-Web import)

```bash
python3 verify_pdf_metadata.py --dir output/api_kartini_files
```

Writes:
- `output/api_kartini_files/metadata_audit.json`
- `output/api_kartini_files/metadata_audit.csv`

Required for `ok` status in the audit: PDF Info `Title`, `Author`, `Company` (publisher), and XMP presence.