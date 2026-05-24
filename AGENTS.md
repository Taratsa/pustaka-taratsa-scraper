# pustaka-taratsa-scraper

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