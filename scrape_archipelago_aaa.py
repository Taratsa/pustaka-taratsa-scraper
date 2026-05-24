#!/usr/bin/env python3
"""
Scraper for https://archive.org/details/@archipelago-anti-authoritarian-archive/

Uses the `internetarchive` Python library for searching and metadata retrieval.
Downloads PDFs via urllib with URL-encoded paths (handles spaces in filenames).

This scraper:
  1. Searches for items in the favorites collection using internetarchive.
  2. For each item, fetches full metadata via get_item().
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
from typing import Any, Dict, List

import internetarchive

ACCOUNT_ID = "@archipelago-anti-authoritarian-archive"
FAV_COLLECTION_ID = "fav-archipelago-anti-authoritarian-archive"
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


# ---------------------------------------------------------------------------
# Fetch full metadata for all items
# ---------------------------------------------------------------------------

def fetch_all_items(
    *,
    identifiers: List[str],
    retries: int,
    sleep_s: float,
) -> List[Dict[str, Any]]:
    """
    For each identifier, fetch full Item metadata via internetarchive.get_item().
    Returns list of enriched item dicts.
    """
    items: List[Dict[str, Any]] = []
    total = len(identifiers)

    for i, ident in enumerate(identifiers, start=1):
        item = None
        for attempt in range(retries + 1):
            try:
                item = internetarchive.get_item(ident)
                break
            except Exception as e:
                if attempt >= retries:
                    print(f"[{i}/{total}] failed {ident}: {e}", file=sys.stderr)
                    break
                time.sleep(1.0 * (attempt + 1))

        if item is None:
            continue

        meta = item.metadata
        files_list = item.files

        # Extract relevant metadata fields
        row: Dict[str, Any] = {
            "archive_identifier": item.identifier,
            "archive_url": f"https://archive.org/details/{item.identifier}",
            "title": meta.get("title", ""),
            "description": meta.get("description", ""),
            "creator": meta.get("creator", ""),
            "date": meta.get("date", ""),
            "language": meta.get("language", ""),
            "mediatype": meta.get("mediatype", ""),
            "subject": meta.get("subject", ""),
            "identifier_access": meta.get("identifier-access", ""),
            "identifier_ark": meta.get("identifier-ark", ""),
            "uploader": meta.get("uploader", ""),
            "addeddate": meta.get("addeddate", ""),
            "publicdate": meta.get("publicdate", ""),
            "access_restricted_item": meta.get("access-restricted-item", ""),
            "collection": meta.get("collection", []),
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

        # Collect available PDF files with metadata
        pdf_files: List[Dict[str, Any]] = []
        for f in files_list:
            if not isinstance(f, dict):
                continue
            name = f.get("name", "")
            if not name.lower().endswith(".pdf"):
                continue
            pdf_files.append({
                "name": name,
                "format": f.get("format", ""),
                "source": f.get("source", ""),
                "length": f.get("length") or "",
                "title": f.get("title") or "",
            })

        row["_pdf_files"] = pdf_files
        items.append(row)
        print(f"[{i}/{total}] {ident} | {row['mediatype']} | {'RESTRICTED' if row['access_restricted_item'] else 'ok'}")

        if sleep_s > 0 and i < total:
            time.sleep(sleep_s)

    return items


# ---------------------------------------------------------------------------
# Download PDFs
# ---------------------------------------------------------------------------

def download_pdfs(
    *,
    items: List[Dict[str, Any]],
    files_dir: str,
    sleep_s: float,
    embed_metadata: bool,
    embed_backup: bool,
) -> List[Dict[str, Any]]:
    """
    Download PDFs for each item using internetarchive's item.download().
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
        subject = item["subject"] or []
        keywords = ", ".join(str(s) for s in subject if s)
        description = str(item["description"] or "")
        restricted = item.get("access_restricted_item") == "true"
        pdf_files = item.get("_pdf_files", [])

        if description:
            subject_str = description
        elif date:
            subject_str = f"{PUBLISHER} · {date}"
        else:
            subject_str = PUBLISHER

        # Select the best PDF
        # Prefer: original source > Text PDF > other
        # Skip: ACS Encrypted PDF (restricted)
        best_pdf: Dict[str, Any] = {}
        for pdf in pdf_files:
            format_ = pdf.get("format", "")
            if format_ == "ACS Encrypted PDF":
                continue
            if not best_pdf:
                best_pdf = pdf
            elif pdf.get("source") == "original" and best_pdf.get("source") != "original":
                best_pdf = pdf
            elif format_ == "Text PDF" and best_pdf.get("format") != "Text PDF":
                best_pdf = pdf

        if not best_pdf:
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
                "error": "no accessible PDF (encrypted or missing)",
            })
            print(f"[pdf] {i}/{n} {identifier} -> no accessible PDF (restricted={restricted})")
            continue

        pdf_name = best_pdf["name"]
        # Sanitize filename - replace spaces and special chars for local path
        safe_name = pdf_name.replace(" ", "_")
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
                "source_url": f"https://archive.org/download/{identifier}/{pdf_name}",
                "file_path": dest,
                "file_format": pdf_name.rsplit(".", 1)[-1] if "." in pdf_name else "pdf",
                "status": "skipped",
                "error": None,
            })
            print(f"[pdf] {i}/{n} {identifier} -> {os.path.basename(dest)} (skipped)")

            # Embed metadata
            if embed_metadata and dest.lower().endswith(".pdf"):
                _embed_pdf(dest, title, creator, subject_str, keywords)
            continue

        # Download via direct urllib request (handles spaces/special chars in filenames)
        try:
            url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(pdf_name)}"
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
                "source_url": f"https://archive.org/download/{identifier}/{pdf_name}",
                "file_path": None,
                "file_format": pdf_name.rsplit(".", 1)[-1] if "." in pdf_name else "pdf",
                "status": "failed",
                "error": str(e),
            })
            print(f"[pdf] failed {i}/{n} {identifier}: {e}", file=sys.stderr)
            if sleep_s > 0:
                time.sleep(sleep_s)
            continue

        # Download succeeded
        manifest.append({
            "archive_identifier": identifier,
            "title": title,
            "creator": creator,
            "date": date,
            "language": language,
            "mediatype": item.get("mediatype", ""),
            "source_url": f"https://archive.org/download/{identifier}/{pdf_name}",
            "file_path": dest,
            "file_format": pdf_name.rsplit(".", 1)[-1] if "." in pdf_name else "pdf",
            "status": "downloaded",
            "error": None,
        })
        print(f"[pdf] {i}/{n} {identifier} -> {os.path.basename(dest)}")

        if embed_metadata and dest.lower().endswith(".pdf"):
            _embed_pdf(dest, title, creator, subject_str, keywords)

        if sleep_s > 0:
            time.sleep(sleep_s)

    return manifest


def _embed_pdf(path: str, title: str, author: str, subject: str, keywords: str) -> None:
    """Embed metadata into a PDF file."""
    from pdf_metadata_embed import embed_pdf_metadata
    try:
        embed_pdf_metadata(
            path,
            title=title,
            author=author,
            subject=subject,
            keywords=keywords,
            publisher=PUBLISHER,
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
        item_copy = {k: v for k, v in item.items() if not k.startswith("_")}
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
        description=f"Scrape archive.org account {ACCOUNT_ID} via its Favorites collection"
    )
    parser.add_argument("--out-dir", default="output/archipelago-aaa", help="books.json / books.csv")
    parser.add_argument("--timeout-s", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep-s", type=float, default=0.15)

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

    # Search for items in the favorites collection
    print(f"Searching for items in collection:{FAV_COLLECTION_ID}...")
    search_results = search_items(
        query=f"collection:{FAV_COLLECTION_ID}",
        retries=args.retries,
        sleep_s=args.sleep_s,
    )
    identifiers = [r["identifier"] for r in search_results if r.get("identifier")]
    print(f"Found {len(identifiers)} items: {identifiers}")

    # Fetch full metadata for each item
    print("Fetching full metadata for each item...")
    items = fetch_all_items(
        identifiers=identifiers,
        retries=args.retries,
        sleep_s=args.sleep_s,
    )

    meta = {
        "account_id": ACCOUNT_ID,
        "favorites_collection": FAV_COLLECTION_ID,
        "item_count": len(items),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    print(f"Total: {len(items)} items.")
    write_outputs(out_dir=args.out_dir, meta=meta, items=items)
    print(f"Wrote: {os.path.join(args.out_dir, 'books.json')} and books.csv")

    if args.download_pdfs:
        manifest = download_pdfs(
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