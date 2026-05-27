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
# Fetch single item metadata + select best PDF/EPUB
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

    # Pre-select the best PDF/EPUB for this item
    row["_best_file"] = _select_best_file(ebext_files)
    return row


def _select_best_file(ebext_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Select the best PDF/EPUB from a list of file entries.
    Prefer: original source > Text PDF > Text EPUB > other.
    Skip: ACS Encrypted PDF (restricted).
    """
    best: Dict[str, Any] = {}
    for f in ebext_files:
        format_ = f.get("format", "")
        if format_ == "ACS Encrypted PDF":
            continue
        if not best:
            best = f
        elif f.get("source") == "original" and best.get("source") != "original":
            best = f
        elif format_ == "Text PDF" and best.get("format") not in ("Text PDF", "Text EPUB"):
            best = f
        elif format_ == "Text EPUB" and best.get("format") not in ("Text PDF", "Text EPUB"):
            best = f
    return best


def fetch_all_items(
    *,
    identifiers: List[str],
    retries: int,
    max_workers: int = 2,
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

        # Use description as subject if available and meaningful, otherwise build from publisher/date
        if description and description not in ("-", "", "None"):
            subject_str = description
        elif publisher:
            subject_str = f"{publisher} · {date}" if date else publisher
        elif date:
            subject_str = f"{PUBLISHER} · {date}"
        else:
            subject_str = PUBLISHER

        # Use pre-selected best file (populated during metadata fetch via _select_best_file)
        best_file = item.get("_best_file") or {}
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

    # Strip internal keys before writing (but keep _ebext_files for incremental re-runs)
    items_out = []
    for item in items:
        item_copy = dict(item)
        # _ebext_files is needed by download_ebext() on subsequent runs when
        # items are loaded from books.json cache instead of freshly fetched.
        item_copy.pop("imagecount", None)
        item_copy.pop("scanner", None)
        item_copy.pop("ppi", None)
        item_copy.pop("ocr_detected_lang", None)
        item_copy.pop("ocr_autonomous", None)
        item_copy.pop("identifier_access", None)
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
    parser.add_argument("--max-workers", type=int, default=16, help="Deprecated — unused, sequential per-item processing")

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
    files_dir = args.files_dir
    os.makedirs(files_dir, exist_ok=True)

    # Load existing items to enable incremental fetch
    existing_items: Dict[str, Dict[str, Any]] = {}

    if args.force_refresh:
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

    # ---- Per-item sequential processing ----
    # For each item: 1. fetch metadata (if new/forced) → 2. download → 3. embed → 4. write
    all_items: List[Dict[str, Any]] = []
    manifest: List[Dict[str, Any]] = []
    n = len(all_identifiers)

    for i, ident in enumerate(all_identifiers, start=1):
        item: Dict[str, Any]

        # Step 1: Get item metadata
        if args.force_refresh or ident not in existing_items:
            row = _fetch_single_item(ident, args.retries)
            if row is None:
                print(f"[{i}/{n}] {ident} | FAILED metadata fetch, skipping")
                continue
            item = row
            if i % 50 == 0 or i == 1:
                print(f"[{i}/{n}] {ident} | {item['mediatype']} | RESTRICTED={bool(item['access_restricted_item'])}")
        else:
            cached = existing_items[ident]
            # Ensure cached item has _best_file (back-compat for items cached before this fix).
            # If _ebext_files is also missing/None, treat as cache miss and refetch from archive.org.
            if "_best_file" not in cached or cached.get("_ebext_files") is None:
                row = _fetch_single_item(ident, args.retries)
                if row is None:
                    print(f"[{i}/{n}] {ident} | FAILED metadata re-fetch, skipping")
                    continue
                item = row
                if i % 50 == 0 or i == 1:
                    print(f"[{i}/{n}] {ident} | {item['mediatype']} | RESTRICTED={bool(item['access_restricted_item'])}")
            else:
                item = cached

        all_items.append(item)

        # Step 2: Download (if enabled)
        if args.download_pdfs:
            file_manifest = _download_single(
                item=item,
                files_dir=files_dir,
                embed_metadata=not args.no_embed_pdf_metadata,
                embed_backup=args.embed_backup,
                index=i,
                total=n,
            )
            if file_manifest:
                manifest.append(file_manifest)

        # Step 3: Write books.json incrementally (flush every 100 items)
        if i % 100 == 0 or i == n:
            _flush_outputs(args.out_dir, all_items, manifest)
            print(f"[flush] {i}/{n} items written to books.json")

    # Final flush
    meta = {
        "account_id": ACCOUNT_ID,
        "uploader_email": UPLOADER_EMAIL,
        "item_count": len(all_items),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _flush_outputs(args.out_dir, all_items, manifest, meta=meta)
    print(f"Done. {len(all_items)} items, {len(manifest)} files processed.")
    write_files_manifest(files_dir=files_dir, manifest=manifest)
    print(f"Wrote: {os.path.join(files_dir, 'files_manifest.json')}")


def _download_single(
    *,
    item: Dict[str, Any],
    files_dir: str,
    embed_metadata: bool,
    embed_backup: bool,
    index: int,
    total: int,
) -> Optional[Dict[str, Any]]:
    """Download, embed metadata, and return a manifest entry for a single item."""
    from pdf_metadata_embed import embed_pdf_metadata

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
    best_file = item.get("_best_file") or {}

    # Build subject string
    if description and description not in ("-", "", "None"):
        subject_str = description
    elif publisher:
        subject_str = f"{publisher} · {date}" if date else publisher
    elif date:
        subject_str = f"{PUBLISHER} · {date}"
    else:
        subject_str = PUBLISHER

    if not best_file:
        print(f"[file] {index}/{total} {identifier} -> no accessible PDF/EPUB (restricted={restricted})")
        return {
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
        }

    file_name = best_file["name"]
    safe_name = file_name.replace(" ", "_")
    dest = os.path.join(files_dir, f"{identifier}_{safe_name}")

    # Check if already downloaded
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        manifest_entry = {
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
        }
        if embed_metadata and dest.lower().endswith(".pdf"):
            _embed_pdf(dest, title, creator, publisher, date, subject_str, keywords)
        return manifest_entry

    # Download
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
        print(f"[file] failed {index}/{total} {identifier}: {e}", file=sys.stderr)
        return {
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
        }

    print(f"[file] {index}/{total} {identifier} -> {os.path.basename(dest)}")
    if embed_metadata and dest.lower().endswith(".pdf"):
        _embed_pdf(dest, title, creator, publisher, date, subject_str, keywords)

    return {
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
    }


def _flush_outputs(
    out_dir: str,
    items: List[Dict[str, Any]],
    manifest: List[Dict[str, Any]],
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Write books.json incrementally.
    Strips noisy technical metadata fields but preserves _ebext_files and _best_file
    so subsequent runs can load from cache without re-fetching.
    """
    os.makedirs(out_dir, exist_ok=True)

    # Strip noisy technical keys but keep _ebext_files and _best_file for cache re-use
    items_out = []
    for item in items:
        item_copy = dict(item)
        item_copy.pop("imagecount", None)
        item_copy.pop("scanner", None)
        item_copy.pop("ppi", None)
        item_copy.pop("ocr_detected_lang", None)
        item_copy.pop("ocr_autonomous", None)
        item_copy.pop("identifier_access", None)
        items_out.append(item_copy)

    # Build meta if not provided
    if meta is None:
        meta = {
            "account_id": ACCOUNT_ID,
            "uploader_email": UPLOADER_EMAIL,
            "item_count": len(items),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    with open(os.path.join(out_dir, "books.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "books": items_out}, f, ensure_ascii=False, indent=2)

    keys = set()
    for b in items_out:
        keys.update(b.keys())

    preferred = [
        "archive_identifier", "archive_url", "title", "description", "creator",
        "date", "language", "mediatype", "subject", "collection", "uploader",
        "addeddate", "publicdate", "identifier_ark", "access_restricted_item",
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


if __name__ == "__main__":
    main()