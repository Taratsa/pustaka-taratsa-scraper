#!/usr/bin/env python3
"""
Scraper for https://archive.org/details/@archipelago-anti-authoritarian-archive/

Uses the `internetarchive` Python library for metadata retrieval.
Searches via archive.org advanced search API using the uploader email.

This scraper:
  1. Searches for items by uploader email via archive.org API.
  2. For each item, fetches full metadata via internetarchive.get_item().
  3. Optionally downloads PDF files.
  4. Writes books.json + books.csv + optional files manifest.
"""
import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed as concurrent_futures_as_completed
from typing import Any, Dict, List, Optional

import internetarchive

ACCOUNT_ID = "@archipelago-anti-authoritarian-archive"
UPLOADER_EMAIL = "archipelagoanarchistarchive@protonmail.com"
PUBLISHER = "Archipelago Anti-Authoritarian Archive"


# ---------------------------------------------------------------------------
# Fetch items via search
# ---------------------------------------------------------------------------

def search_items(
    *,
    query: str,
    retries: int,
    sleep_s: float,
) -> List[Dict[str, Any]]:
    """
    Search archive.org using the internetarchive library.
    Returns list of item dicts with identifier + mediatype.
    """
    results: List[Dict[str, Any]] = []
    attempt = 0
    while attempt <= retries:
        try:
            search = internetarchive.search_items(query)
            for result in search:
                results.append(dict(result))
            return results
        except Exception as e:
            attempt += 1
            if attempt > retries:
                raise RuntimeError(f"search_items failed after {retries} retries: {e}") from e
            time.sleep(sleep_s * attempt)


def get_all_identifiers_via_api(
    *,
    uploader_email: str,
    retries: int = 3,
) -> List[str]:
    """
    Fetch all item identifiers for a given uploader email via archive.org API.
    Uses the advanced search API which supports uploader:email queries.
    """
    import requests

    identifiers: List[str] = []
    rows = 1000
    page = 1

    while True:
        try:
            resp = requests.get(
                "https://archive.org/advancedsearch.php",
                params={
                    "q": f"uploader:{uploader_email}",
                    "fl[]": ["identifier"],
                    "output": "json",
                    "rows": rows,
                    "page": page,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            docs = data.get("response", {}).get("docs", [])
            if not docs:
                break
            for doc in docs:
                ident = doc.get("identifier")
                if ident:
                    identifiers.append(ident)
            total = data.get("response", {}).get("numFound", 0)
            print(f"[search] page {page}: got {len(docs)} items ({len(identifiers)}/{total})")
            if len(identifiers) >= total:
                break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            if retries <= 0:
                raise RuntimeError(f"get_all_identifiers_via_api failed: {e}") from e
            print(f"[search] retry {retries}: {e}")
            time.sleep(1)
            return get_all_identifiers_via_api(uploader_email=uploader_email, retries=retries - 1)

    return identifiers


# ---------------------------------------------------------------------------
# Fetch full metadata for all items
# ---------------------------------------------------------------------------

def _fetch_single_item(ident: str, retries: int) -> Optional[Dict[str, Any]]:
    """Fetch metadata for a single identifier. Returns None on failure."""
    for attempt in range(retries + 1):
        try:
            item = internetarchive.get_item(ident)
            return _item_to_row(item)
        except Exception as e:
            if attempt >= retries:
                print(f"  [fail] {ident}: {e}", file=sys.stderr)
                return None
    return None


def _item_to_row(item: Any) -> Dict[str, Any]:
    """Convert an internetarchive Item to a metadata row dict."""
    meta = item.metadata
    files_list = item.files

    row: Dict[str, Any] = {
        # Core identifiers
        "archive_identifier": item.identifier,
        "archive_url": f"https://archive.org/details/{item.identifier}",
        "cover_url": f"https://archive.org/services/img/{item.identifier}",
        "download_url": f"https://archive.org/download/{item.identifier}",
        # Basic metadata
        "title": meta.get("title", ""),
        "creator": meta.get("creator", ""),
        "publisher": meta.get("publisher", ""),
        "date": meta.get("date", ""),
        "language": meta.get("language", ""),
        "mediatype": meta.get("mediatype", ""),
        "description": meta.get("description", ""),
        # Subject/keywords
        "subject": meta.get("subject", ""),
        "rights": meta.get("rights", ""),
        # Archive.org specific
        "identifier_access": meta.get("identifier-access", ""),
        "identifier_ark": meta.get("identifier-ark", ""),
        "uploader": meta.get("uploader", ""),
        "addeddate": meta.get("addeddate", ""),
        "publicdate": meta.get("publicdate", ""),
        "access_restricted_item": meta.get("access-restricted-item", ""),
        "collection": meta.get("collection", []),
        # Technical metadata
        "imagecount": meta.get("imagecount", ""),
        "scanner": meta.get("scanner", ""),
        "ppi": meta.get("ppi", ""),
        # OCR info
        "ocr_detected_lang": meta.get("ocr_detected_lang", ""),
        "ocr_autonomous": meta.get("ocr_autonomous", ""),
    }

    # Normalize subject
    subj = row["subject"]
    if isinstance(subj, str):
        row["subject"] = [s.strip() for s in subj.split(",") if s.strip()]
    elif isinstance(subj, list):
        row["subject"] = [str(s) for s in subj]
    else:
        row["subject"] = []

    # Normalize collection
    coll = row["collection"]
    if isinstance(coll, str):
        row["collection"] = [c.strip() for c in coll.split(",") if c.strip()]
    elif isinstance(coll, list):
        row["collection"] = [str(c) for c in coll]
    else:
        row["collection"] = []

    # Collect available PDF and EPUB files with metadata
    ebext_files: List[Dict[str, Any]] = []
    for f in files_list:
        if not isinstance(f, dict):
            continue
        name = f.get("name", "")
        if not (name.lower().endswith(".pdf") or name.lower().endswith(".epub")):
            continue
        ebext_files.append({
            "name": name,
            "format": f.get("format", ""),
            "source": f.get("source", ""),
            "length": f.get("length") or "",
            "title": f.get("title") or "",
        })

    row["_ebext_files"] = ebext_files
    return row


def fetch_all_items(
    *,
    identifiers: List[str],
    retries: int,
    max_workers: int = 8,
) -> List[Dict[str, Any]]:
    """
    Fetch full Item metadata for all identifiers in parallel.
    Uses ThreadPoolExecutor for concurrent requests.
    """
    total = len(identifiers)
    items: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_single_item, ident, retries): ident
            for ident in identifiers
        }
        for i, future in enumerate(concurrent_futures_as_completed(futures), start=1):
            ident = futures[future]
            result = future.result()
            if result is not None:
                items.append(result)
                print(f"[{i}/{total}] {ident} | {result['mediatype']} | {'RESTRICTED' if result['access_restricted_item'] else 'ok'}")
            else:
                print(f"[{i}/{total}] {ident} | FAILED")

    # Preserve original order
    id_to_row = {row["archive_identifier"]: row for row in items}
    ordered = [id_to_row[ident] for ident in identifiers if ident in id_to_row]
    return ordered

    return items


# ---------------------------------------------------------------------------
# Download PDFs/EPUBs
# ---------------------------------------------------------------------------

def download_ebext(
    *,
    items: List[Dict[str, Any]],
    files_dir: str,
    sleep_s: float,
    embed_metadata: bool,
    embed_backup: bool,
) -> List[Dict[str, Any]]:
    """
    Download PDFs and EPUBs for each item.
    Handles restricted items gracefully.
    """
    # Import here to avoid top-level import issues
    from pdf_metadata_embed import embed_pdf_metadata

    os.makedirs(files_dir, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    n = len(items)

    for i, item in enumerate(items, start=1):
        identifier = item["archive_identifier"]
        title = str(item["title"] or identifier)
        creator = str(item["creator"] or "")
        date = str(item["date"] or "")
        language = str(item["language"] or "")
        publisher = str(item.get("publisher") or "")
        subject = item["subject"] or []
        keywords = ", ".join(str(s) for s in subject if s)
        description = str(item["description"] or "")
        restricted = item.get("access_restricted_item") == "true"
        ebext_files = item.get("_ebext_files", [])

        # Use description as subject if available and meaningful, otherwise build from publisher/date
        if description and description not in ("-", "", "None"):
            subject_str = description
        elif publisher:
            subject_str = f"{publisher} · {date}" if date else publisher
        elif date:
            subject_str = f"{PUBLISHER} · {date}"
        else:
            subject_str = PUBLISHER

        # Select the best PDF/EPUB
        # Prefer: original source > Text PDF > Text EPUB > other
        # Skip: ACS Encrypted PDF (restricted)
        best_file: Dict[str, Any] = {}
        for f in ebext_files:
            format_ = f.get("format", "")
            if format_ == "ACS Encrypted PDF":
                continue
            if not best_file:
                best_file = f
            elif f.get("source") == "original" and best_file.get("source") != "original":
                best_file = f
            elif format_ == "Text PDF" and best_file.get("format") != "Text PDF":
                best_file = f
            elif format_ == "Text EPUB" and best_file.get("format") not in ("Text PDF", "Text EPUB"):
                best_file = f

        if not best_file:
            manifest.append({
                "archive_identifier": identifier,
                "title": title,
                "creator": creator,
                "date": date,
                "language": language,
                "mediatype": item.get("mediatype", ""),
                "source_url": None,
                "file_path": None,
                "file_format": None,
                "status": "failed",
                "error": "no accessible PDF/EPUB (encrypted or missing)",
            })
            print(f"[file] {i}/{n} {identifier} -> no accessible PDF/EPUB (restricted={restricted})")
            continue

        file_name = best_file["name"]
        # Sanitize filename - replace spaces and special chars for local path
        safe_name = file_name.replace(" ", "_")
        dest = os.path.join(files_dir, f"{identifier}_{safe_name}")

        # Check if already downloaded
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            manifest.append({
                "archive_identifier": identifier,
                "title": title,
                "creator": creator,
                "date": date,
                "language": language,
                "mediatype": item.get("mediatype", ""),
                "source_url": f"https://archive.org/download/{identifier}/{file_name}",
                "file_path": dest,
                "file_format": file_name.rsplit(".", 1)[-1] if "." in file_name else "pdf",
                "status": "skipped",
                "error": None,
            })
            print(f"[file] {i}/{n} {identifier} -> {os.path.basename(dest)} (skipped)")

            # Embed metadata into PDF only (EPUB metadata embedding not implemented)
            if embed_metadata and dest.lower().endswith(".pdf"):
                _embed_pdf(dest, title, creator, publisher, date, subject_str, keywords)
            continue

        # Download via direct urllib request (handles spaces/special chars in filenames)
        try:
            url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(file_name)}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; scrape-archipelago-aaa/1.0)"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 128)
                        if not chunk:
                            break
                        f.write(chunk)
            if os.path.getsize(dest) == 0:
                os.remove(dest)
                raise RuntimeError("downloaded file is 0 bytes")
        except Exception as e:
            manifest.append({
                "archive_identifier": identifier,
                "title": title,
                "creator": creator,
                "date": date,
                "language": language,
                "mediatype": item.get("mediatype", ""),
                "source_url": f"https://archive.org/download/{identifier}/{file_name}",
                "file_path": None,
                "file_format": file_name.rsplit(".", 1)[-1] if "." in file_name else "pdf",
                "status": "failed",
                "error": str(e),
            })
            print(f"[file] failed {i}/{n} {identifier}: {e}", file=sys.stderr)
            continue

        # Download succeeded
        manifest.append({
            "archive_identifier": identifier,
            "title": title,
            "creator": creator,
            "date": date,
            "language": language,
            "mediatype": item.get("mediatype", ""),
            "source_url": f"https://archive.org/download/{identifier}/{file_name}",
            "file_path": dest,
            "file_format": file_name.rsplit(".", 1)[-1] if "." in file_name else "pdf",
            "status": "downloaded",
            "error": None,
        })
        print(f"[file] {i}/{n} {identifier} -> {os.path.basename(dest)}")

        if embed_metadata and dest.lower().endswith(".pdf"):
            _embed_pdf(dest, title, creator, publisher, date, subject_str, keywords)

    return manifest


def _embed_pdf(path: str, title: str, author: str, publisher: str, date: str, subject: str, keywords: str) -> None:
    """Embed metadata into a PDF file."""
    from pdf_metadata_embed import embed_pdf_metadata
    try:
        embed_pdf_metadata(
            path,
            title=title,
            author=author,
            subject=subject,
            keywords=keywords,
            publisher=publisher if publisher else PUBLISHER,
            creation_date=date,
            write_xmp=True,
            backup=False,
        )
        print(f"[pdf-meta] {os.path.basename(path)}")
    except Exception as e:
        print(f"[pdf-meta] failed {os.path.basename(path)}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Write outputs
# ---------------------------------------------------------------------------

def write_outputs(
    *,
    out_dir: str,
    meta: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> None:
    os.makedirs(out_dir, exist_ok=True)

    # Strip internal _pdf_files key before writing
    items_out = []
    for item in items:
        item_copy = dict(item)
        item_copy.pop("_ebext_files", None)
        items_out.append(item_copy)

    with open(os.path.join(out_dir, "books.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "books": items_out}, f, ensure_ascii=False, indent=2)

    keys = set()
    for b in items_out:
        keys.update(b.keys())

    preferred = [
        "archive_identifier",
        "archive_url",
        "title",
        "description",
        "creator",
        "date",
        "language",
        "mediatype",
        "subject",
        "collection",
        "uploader",
        "addeddate",
        "publicdate",
        "identifier_ark",
        "access_restricted_item",
    ]
    fieldnames = [k for k in preferred if k in keys] + sorted(keys - set(preferred))

    with open(os.path.join(out_dir, "books.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for b in items_out:
            row = dict(b)
            subj = row.get("subject")
            if isinstance(subj, list):
                row["subject"] = "|".join(str(s) for s in subj)
            coll = row.get("collection")
            if isinstance(coll, list):
                row["collection"] = "|".join(str(c) for c in coll)
            w.writerow(row)


def write_files_manifest(
    *,
    files_dir: str,
    manifest: List[Dict[str, Any]],
) -> None:
    os.makedirs(files_dir, exist_ok=True)
    jpath = os.path.join(files_dir, "files_manifest.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    cpath = os.path.join(files_dir, "files_manifest.csv")
    fields = [
        "archive_identifier",
        "title",
        "creator",
        "date",
        "language",
        "mediatype",
        "source_url",
        "file_path",
        "file_format",
        "status",
        "error",
    ]
    with open(cpath, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in manifest:
            w.writerow(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Scrape archive.org account {ACCOUNT_ID} via uploader email"
    )
    parser.add_argument("--out-dir", default="output/archipelago-aaa", help="books.json / books.csv")
    parser.add_argument("--timeout-s", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep-s", type=float, default=0.05, help="Deprecated — unused, kept for compat")
    parser.add_argument("--max-workers", type=int, default=16, help="Parallel workers for metadata fetch")

    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-fetch metadata for ALL items (not just new ones). By default, only new items are fetched.",
    )

    parser.add_argument(
        "--download-pdfs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download missing PDFs into --files-dir (default: enabled). Use --no-download-pdfs to skip.",
    )
    parser.add_argument("--files-dir", default="output/archipelago-aaa/files")
    parser.add_argument("--pdfs-sleep-s", type=float, default=0.1)
    parser.add_argument(
        "--no-embed-pdf-metadata",
        action="store_true",
        help="Do not embed title/author/subject/keywords (+XMP) into downloaded PDFs",
    )
    parser.add_argument("--embed-backup", action="store_true", help="Create .bak before embedding")

    args = parser.parse_args()

    books_json_path = os.path.join(args.out_dir, "books.json")

    # Load existing items to enable incremental fetch
    existing_items: Dict[str, Dict[str, Any]] = {}

    if args.force_refresh:
        # Full refresh: ignore existing cache
        print(f"[refresh] Ignoring existing cache, will fetch all items from archive.org")
    elif os.path.exists(books_json_path):
        with open(books_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        existing_items = {item["archive_identifier"]: item for item in data.get("books", [])}
        print(f"[load] Loaded {len(existing_items)} existing items from {books_json_path}")

    # Search for all identifiers (to discover new ones added since last run)
    print(f"Searching for items by uploader:{UPLOADER_EMAIL}...")
    all_identifiers = get_all_identifiers_via_api(uploader_email=UPLOADER_EMAIL, retries=args.retries)
    print(f"Found {len(all_identifiers)} items total")

    if args.force_refresh:
        # Re-fetch all items
        print(f"Fetching metadata for all {len(all_identifiers)} items...")
        items = fetch_all_items(
            identifiers=all_identifiers,
            retries=args.retries,
            max_workers=args.max_workers,
        )
        new_items = items
    else:
        # Determine which identifiers are new (not in existing cache)
        new_identifiers = [ident for ident in all_identifiers if ident not in existing_items]
        print(f"[diff] {len(existing_items)} cached + {len(new_identifiers)} new")

        # Fetch metadata only for new identifiers
        if new_identifiers:
            print(f"Fetching metadata for {len(new_identifiers)} new items...")
            new_items = fetch_all_items(
                identifiers=new_identifiers,
                retries=args.retries,
                max_workers=args.max_workers,
            )
            print(f"Fetched {len(new_items)} new items")
        else:
            new_items = []

        # Build ordered list: existing items (preserving archive.org sort order) + new items
        all_items_dict = dict(existing_items)
        for item in new_items:
            all_items_dict[item["archive_identifier"]] = item
        items = [all_items_dict[ident] for ident in all_identifiers if ident in all_items_dict]

    meta = {
        "account_id": ACCOUNT_ID,
        "uploader_email": UPLOADER_EMAIL,
        "item_count": len(items),
        "cached_count": len(existing_items),
        "new_count": len(new_items),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    print(f"Total: {len(items)} items ({len(existing_items)} cached + {len(new_items)} new).")
    write_outputs(out_dir=args.out_dir, meta=meta, items=items)
    print(f"Wrote: {os.path.join(args.out_dir, 'books.json')} and books.csv")

    if args.download_pdfs:
        manifest = download_ebext(
            items=items,
            files_dir=args.files_dir,
            sleep_s=args.pdfs_sleep_s,
            embed_metadata=not args.no_embed_pdf_metadata,
            embed_backup=args.embed_backup,
        )
        write_files_manifest(files_dir=args.files_dir, manifest=manifest)
        print(f"Wrote: {os.path.join(args.files_dir, 'files_manifest.json')}")


if __name__ == "__main__":
    main()