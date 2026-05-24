#!/usr/bin/env python3
import argparse
import csv
import json
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from pdf_metadata_embed import embed_pdf_metadata


BASE_URL = "https://langka.logosid.app/api/books"
DEFAULT_PAGE_SIZE = 20


def _build_url(page: int, page_size: int) -> str:
    return f"{BASE_URL}?page={page}&pageSize={page_size}"


def _http_get_json(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> Dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; scrape-langka-logos/1.0)",
        "Accept": "application/json",
    }

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                raw = resp.read().decode(charset, errors="replace")
                return json.loads(raw)
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as e:
            last_err = e
            if attempt >= retries:
                break
            sleep_s = backoff_s * (2**attempt) + random.uniform(0, 0.25)
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _slugify(s: str, *, max_len: int = 80) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "book"


def _download_bytes(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> Tuple[bytes, Optional[str]]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-langka-logos/1.0)"}
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                content_type = resp.headers.get("Content-Type")
                return resp.read(), content_type
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as e:
            last_err = e
            if attempt >= retries:
                break
            sleep_s = backoff_s * (2**attempt) + random.uniform(0, 0.25)
            time.sleep(sleep_s)
    assert last_err is not None
    raise last_err


def _guess_extension(content_type: Optional[str]) -> str:
    if not content_type:
        return "img"
    content_type = content_type.split(";")[0].strip().lower()
    if content_type in ("application/pdf", "application/x-pdf"):
        return "pdf"
    if content_type in ("text/html", "application/xhtml+xml"):
        return "html"
    if content_type in ("image/jpeg", "image/jpg"):
        return "jpg"
    if content_type == "image/png":
        return "png"
    if content_type == "image/gif":
        return "gif"
    if content_type == "image/webp":
        return "webp"
    if content_type == "image/svg+xml":
        return "svg"
    return "img"


def _extract_google_drive_file_id(url: str) -> Optional[str]:
    # Common formats:
    # - https://drive.google.com/file/d/<id>/view?usp=sharing
    # - https://drive.google.com/open?id=<id>
    # - https://drive.google.com/uc?id=<id>&export=download
    patterns = [
        r"drive\.google\.com\/file\/d\/([A-Za-z0-9_-]+)",
        r"drive\.google\.com\/open\?id=([A-Za-z0-9_-]+)",
        r"drive\.google\.com\/uc\?id=([A-Za-z0-9_-]+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _try_filename_from_content_disposition(content_disposition: Optional[str]) -> Optional[str]:
    # e.g. Content-Disposition: attachment; filename="file.pdf"
    if not content_disposition:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', content_disposition, flags=re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    # Remove any path components to avoid directory traversal.
    name = name.replace("\\", "/").split("/")[-1]
    return name or None


def _already_downloaded_prefix(out_dir: str, prefix: str) -> Optional[str]:
    if not os.path.isdir(out_dir):
        return None
    for name in os.listdir(out_dir):
        if name.startswith(prefix + ".") or name.startswith(prefix):
            return os.path.join(out_dir, name)
    return None


def _download_to_path(
    url: str,
    file_path: str,
    *,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    insecure_ssl: bool = False,
) -> Optional[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-langka-logos/1.0)"}
    context = ssl._create_unverified_context() if insecure_ssl else None

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            if context is None:
                resp_cm = urllib.request.urlopen(req, timeout=timeout_s)
            else:
                resp_cm = urllib.request.urlopen(req, timeout=timeout_s, context=context)
            with resp_cm as resp:
                content_type = resp.headers.get("Content-Type")
                content_disposition = resp.headers.get("Content-Disposition")
                # Stream to disk (avoid loading PDFs fully in memory).
                tmp_path = file_path + ".part"
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 128)
                        if not chunk:
                            break
                        f.write(chunk)
                os.replace(tmp_path, file_path)
                # Some hosts return an HTML shell with an empty body; don't keep 0-byte artifacts.
                if os.path.exists(file_path) and os.path.getsize(file_path) == 0:
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                    raise RuntimeError("downloaded file is 0 bytes")
                filename = _try_filename_from_content_disposition(content_disposition)
                # Prefer filename extension if available.
                if filename and "." in filename:
                    return filename.split(".")[-1].lower()
                return _guess_extension(content_type)
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as e:
            last_err = e
            if attempt >= retries:
                break
            # Small exponential backoff + jitter.
            sleep_s = backoff_s * (2**attempt) + random.uniform(0, 0.25)
            time.sleep(sleep_s)
            continue
        finally:
            # Clean up partial downloads if we failed mid-stream.
            tmp_path = file_path + ".part"
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    assert last_err is not None
    raise last_err


def _download_google_drive_file(
    file_id: str,
    *,
    out_dir: str,
    prefix: str,
    url_title_slug: str,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
    insecure_ssl: bool = False,
) -> Optional[str]:
    os.makedirs(out_dir, exist_ok=True)
    existing = _already_downloaded_prefix(out_dir, prefix)
    if existing:
        # If we previously saved a tiny Google Drive warning HTML, replace it.
        try:
            if existing.lower().endswith(".html") and os.path.getsize(existing) < 1024 * 1024:
                os.remove(existing)
            else:
                return existing
        except OSError:
            return existing

    # Direct download link; this usually avoids the "view" page entirely.
    base_direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"

    # First, probe quickly for content-type / confirm token.
    probe_url = base_direct_url
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-langka-logos/1.0)"}
    context = ssl._create_unverified_context() if insecure_ssl else None
    try:
        req = urllib.request.Request(probe_url, headers=headers, method="GET")
        if context is None:
            resp_cm = urllib.request.urlopen(req, timeout=timeout_s)
        else:
            resp_cm = urllib.request.urlopen(req, timeout=timeout_s, context=context)
        with resp_cm as resp:
            content_type = resp.headers.get("Content-Type") or ""
            if content_type.lower().startswith("text/html"):
                # If Google requires a confirm step, it often returns a page containing `confirm=...`.
                raw = resp.read(256 * 1024).decode("utf-8", errors="replace")
                confirm_m = re.search(r"confirm=([0-9A-Za-z_-]+)", raw)
                if not confirm_m:
                    # Some Drive pages store confirm token as:
                    #   <input type="hidden" name="confirm" value="t">
                    confirm_m = re.search(r'name="confirm"\s+value="([^"]+)"', raw)

                uuid_m = re.search(r'name="uuid"\s+value="([^"]+)"', raw)
                action_m = re.search(r'action="([^"]+)"', raw)

                if confirm_m and uuid_m and action_m:
                    # Virus scan warning pages include a form action URL + both tokens.
                    # Example:
                    #   action="https://drive.usercontent.google.com/download"
                    #   id=<file_id>&export=download&confirm=<token>&uuid=<uuid>
                    confirm = confirm_m.group(1)
                    uuid = uuid_m.group(1)
                    action = action_m.group(1)
                    probe_url = f"{action}?export=download&id={file_id}&confirm={confirm}&uuid={uuid}"
                elif confirm_m:
                    # Fallback: attempt confirm on the standard uc download endpoint.
                    probe_url = base_direct_url + f"&confirm={confirm_m.group(1)}"
    except Exception:
        # We'll just try downloading from the base link next.
        probe_url = base_direct_url

    # Download, letting headers decide extension where possible.
    # Start with an educated default filename; _download_to_path returns the discovered ext.
    tentative_path = os.path.join(out_dir, prefix + ".pdf")
    ext = _download_to_path(
        probe_url,
        tentative_path,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
        insecure_ssl=insecure_ssl,
    )
    final_path = os.path.join(out_dir, prefix + "." + (ext or "pdf"))
    # If we guessed pdf but actual ext differs, rename.
    if final_path != tentative_path:
        if os.path.exists(tentative_path):
            os.replace(tentative_path, final_path)
    if sleep_s > 0:
        time.sleep(sleep_s)
    return final_path


def download_docs(
    *,
    out_dir: str,
    books: List[Dict[str, Any]],
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
    insecure_ssl: bool = False,
) -> List[Dict[str, Any]]:
    os.makedirs(out_dir, exist_ok=True)
    total = len(books)
    manifest: List[Dict[str, Any]] = []
    for i, b in enumerate(books, start=1):
        doc_url = b.get("url")
        if not doc_url:
            continue
        book_id = b.get("id")
        title = str(b.get("title") or "")
        safe = _slugify(title)
        author = str(b.get("author") or "")
        category = b.get("category") or ""
        tags = [category] if category else []

        prefix = f"{book_id}-{safe}" if book_id is not None else safe

        try:
            existing_any = _already_downloaded_prefix(out_dir, prefix)
            if existing_any:
                existing_size = os.path.getsize(existing_any) if os.path.exists(existing_any) else 0
                # Replace tiny Google warning HTML; otherwise skip.
                if existing_size > 0 and not (existing_any.lower().endswith(".html") and existing_size < 1024 * 1024):
                    entry = {
                        "book_id": book_id,
                        "title": title,
                        "author": author,
                        "category": category,
                        "tags": tags,
                        "source_url": str(doc_url),
                        "file_path": existing_any,
                        "status": "skipped",
                    }
                    manifest.append(entry)
                    print(f"[docs] {i}/{total} id={book_id} -> {existing_any} (skipped)")
                    continue
                if existing_size == 0:
                    try:
                        os.remove(existing_any)
                    except OSError:
                        pass

            drive_id = _extract_google_drive_file_id(str(doc_url))
            if drive_id:
                path = _download_google_drive_file(
                    drive_id,
                    out_dir=out_dir,
                    prefix=prefix,
                    url_title_slug=safe,
                    timeout_s=timeout_s,
                    retries=retries,
                    backoff_s=backoff_s,
                    sleep_s=sleep_s,
                    insecure_ssl=insecure_ssl,
                )
                print(f"[docs] {i}/{total} id={book_id} -> {path}")
                if path and os.path.exists(path) and os.path.getsize(path) > 0:
                    entry = {
                        "book_id": book_id,
                        "title": title,
                        "author": author,
                        "category": category,
                        "tags": tags,
                        "source_url": str(doc_url),
                        "file_path": path,
                        "status": "downloaded",
                    }
                    manifest.append(entry)
                else:
                    raise RuntimeError("downloaded file missing or empty")
            else:
                # Fallback for non-Drive URLs (best-effort).
                existing_any = _already_downloaded_prefix(out_dir, prefix)
                if existing_any:
                    try:
                        if os.path.getsize(existing_any) > 0:
                            entry = {
                                "book_id": book_id,
                                "title": title,
                                "author": author,
                                "category": category,
                                "tags": tags,
                                "source_url": str(doc_url),
                                "file_path": existing_any,
                                "status": "skipped",
                            }
                            manifest.append(entry)
                            print(f"[docs] {i}/{total} id={book_id} -> {existing_any} (skipped)")
                            continue
                        os.remove(existing_any)
                    except OSError:
                        pass

                tentative_path = os.path.join(out_dir, prefix + ".bin")
                ext = _download_to_path(
                    str(doc_url),
                    tentative_path,
                    timeout_s=timeout_s,
                    retries=retries,
                    backoff_s=backoff_s,
                    insecure_ssl=insecure_ssl,
                )
                final_path = os.path.join(out_dir, prefix + "." + (ext or "bin"))
                if final_path != tentative_path and os.path.exists(tentative_path):
                    os.replace(tentative_path, final_path)
                if sleep_s > 0:
                    time.sleep(sleep_s)
                print(f"[docs] {i}/{total} id={book_id} -> {final_path}")
                if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
                    entry = {
                        "book_id": book_id,
                        "title": title,
                        "author": author,
                        "category": category,
                        "tags": tags,
                        "source_url": str(doc_url),
                        "file_path": final_path,
                        "status": "downloaded",
                    }
                    manifest.append(entry)
                else:
                    raise RuntimeError("downloaded file missing or empty")
        except Exception as e:
            entry = {
                "book_id": book_id,
                "title": title,
                "author": author,
                "category": category,
                "tags": tags,
                "source_url": str(doc_url),
                "file_path": None,
                "status": "failed",
                "error": str(e),
            }
            manifest.append(entry)
            print(f"[docs] failed {i}/{total} id={book_id}: {e}", file=sys.stderr)

    return manifest


def write_docs_manifest(*, out_dir: str, manifest: List[Dict[str, Any]]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "docs_manifest.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(out_dir, "docs_manifest.csv")
    fieldnames = ["book_id", "title", "author", "category", "tags", "source_url", "file_path", "status", "error"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for entry in manifest:
            row = dict(entry)
            tags = row.get("tags")
            if isinstance(tags, list):
                row["tags"] = "|".join([str(t) for t in tags if t])
            writer.writerow(row)


def write_pdf_metadata_from_manifest(
    *,
    manifest_path: str,
    backup: bool,
    limit: Optional[int],
    write_xmp: bool,
) -> int:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    if not isinstance(manifest, list):
        raise ValueError(f"Manifest must be a JSON list, got: {type(manifest).__name__}")

    updated = 0
    for entry in manifest:
        if limit is not None and updated >= limit:
            break
        if entry.get("status") not in ("downloaded", "skipped"):
            continue
        pdf_path = entry.get("file_path")
        if not pdf_path or not isinstance(pdf_path, str):
            continue
        if not pdf_path.lower().endswith(".pdf"):
            continue
        if not os.path.exists(pdf_path):
            continue

        title = str(entry.get("title") or "")
        author = str(entry.get("author") or "")
        category = str(entry.get("category") or "")
        tags = entry.get("tags") or []
        if isinstance(tags, list):
            keywords = ", ".join([str(t) for t in tags if t])
        else:
            keywords = str(tags)

        try:
            embed_pdf_metadata(
                pdf_path,
                title=title,
                author=author,
                subject=category,
                keywords=keywords,
                write_xmp=write_xmp,
                backup=backup,
            )
        except Exception as e:
            print(f"[pdf-meta] failed {pdf_path}: {e}", file=sys.stderr)
            continue

        updated += 1
        print(f"[pdf-meta] updated: {pdf_path}")

    return updated


def scrape_books(
    *,
    page_size: int,
    start_page: int,
    end_page: Optional[int],
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    first_url = _build_url(start_page, page_size)
    first = _http_get_json(first_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)

    total_pages = int(first.get("totalPages", 1))
    total = int(first.get("total", 0))

    if end_page is None:
        end_page = total_pages
    end_page = int(end_page)
    if start_page < 1:
        raise ValueError("--start-page must be >= 1")
    if end_page < start_page:
        raise ValueError("--end-page must be >= start-page")

    books: List[Dict[str, Any]] = []
    meta = {
        "base_url": BASE_URL,
        "page_size": page_size,
        "total": total,
        "totalPages": total_pages,
        "start_page": start_page,
        "end_page": end_page,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    for page in range(start_page, end_page + 1):
        url = _build_url(page, page_size)
        payload = first if page == start_page else _http_get_json(url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
        page_data = payload.get("data", []) or []
        for b in page_data:
            b = dict(b)
            b["source_page"] = page
            books.append(b)
        if sleep_s > 0 and page != end_page:
            time.sleep(sleep_s)

    return meta, books


def write_outputs(*, out_dir: str, meta: Dict[str, Any], books: List[Dict[str, Any]]) -> None:
    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "books.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "books": books}, f, ensure_ascii=False, indent=2)

    # Keep stable column order (best-effort).
    preferred_order = [
        "id",
        "title",
        "author",
        "category",
        "description",
        "text_markdown",
        "image_url",
        "url",
        "reading_time_hours",
        "is_featured",
        "created_at",
        "category_id",
        "source_page",
    ]

    fieldnames_set = set()
    for b in books:
        fieldnames_set.update(b.keys())

    fieldnames = [k for k in preferred_order if k in fieldnames_set] + sorted(fieldnames_set - set(preferred_order))

    csv_path = os.path.join(out_dir, "books.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for b in books:
            writer.writerow(b)


def download_covers(
    *,
    out_dir: str,
    books: List[Dict[str, Any]],
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for i, b in enumerate(books, start=1):
        image_url = b.get("image_url")
        if not image_url:
            continue

        title = str(b.get("title") or "")
        safe = _slugify(title)
        book_id = b.get("id")
        ext_path = os.path.join(out_dir, f"{book_id}-{safe}")

        # Check if already downloaded: we'll look for any known extension.
        existing = None
        for ext in ("jpg", "png", "gif", "webp", "svg", "img"):
            candidate = f"{ext_path}.{ext}"
            if os.path.exists(candidate):
                existing = candidate
                break
        if existing:
            continue

        try:
            data, content_type = _download_bytes(image_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
            ext = _guess_extension(content_type)
            file_path = f"{ext_path}.{ext}"
            with open(file_path, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[covers] failed {i}/{len(books)} id={book_id}: {e}", file=sys.stderr)
        if sleep_s > 0:
            time.sleep(sleep_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape langka.logosid.app /api/books")
    parser.add_argument("--out-dir", default="output", help="Output directory (default: output)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int, default=None)

    parser.add_argument("--timeout-s", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff-s", type=float, default=0.5)
    parser.add_argument("--sleep-s", type=float, default=0.1, help="Sleep between page requests")

    parser.add_argument("--download-covers", action="store_true", help="Download cover images")
    parser.add_argument("--covers-dir", default="output/covers", help="Covers download directory")
    parser.add_argument("--covers-sleep-s", type=float, default=0.05)

    parser.add_argument(
        "--download-docs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download missing documents from each book url (default: enabled). Use --no-download-docs to skip.",
    )
    parser.add_argument(
        "--download-docs-with-metadata",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Download documents and immediately embed PDF metadata (Info+XMP) from docs_manifest.json",
    )
    parser.add_argument("--docs-dir", default="output/docs", help="Documents download directory")
    parser.add_argument("--docs-sleep-s", type=float, default=0.02)
    parser.add_argument("--insecure-ssl", action="store_true", help="Allow invalid TLS certificates (for some hosts)")

    parser.add_argument("--write-pdf-metadata", action="store_true", help="Write embedded PDF metadata from docs_manifest.json")
    parser.add_argument("--pdf-metadata-manifest", default=None, help="Path to docs_manifest.json (default: <docs-dir>/docs_manifest.json)")
    parser.add_argument("--pdf-metadata-backup", action="store_true", help="Create a .bak backup before rewriting PDFs")
    parser.add_argument("--pdf-metadata-limit", type=int, default=None, help="Update only the first N PDFs (debug/testing)")
    parser.add_argument(
        "--pdf-metadata-calibre-web-xmp",
        action="store_true",
        help="Also write a minimal XMP packet so calibre-web's PDF uploader can read author/title reliably",
    )

    args = parser.parse_args()

    # Only hit the API if we need fresh data for downloads/scraping.
    download_docs_requested = args.download_docs or args.download_docs_with_metadata
    should_scrape = (args.download_covers or download_docs_requested) or (not args.write_pdf_metadata)
    books: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    if should_scrape:
        meta, books = scrape_books(
            page_size=args.page_size,
            start_page=args.start_page,
            end_page=args.end_page,
            timeout_s=args.timeout_s,
            retries=args.retries,
            backoff_s=args.backoff_s,
            sleep_s=args.sleep_s,
        )

        print(f"Fetched {len(books)} books (totalPages={meta['totalPages']}).")
        write_outputs(out_dir=args.out_dir, meta=meta, books=books)
        print(f"Wrote: {os.path.join(args.out_dir, 'books.json')} and {os.path.join(args.out_dir, 'books.csv')}")

        if args.download_covers:
            print(f"Downloading covers into: {args.covers_dir}")
            download_covers(
                out_dir=args.covers_dir,
                books=books,
                timeout_s=args.timeout_s,
                retries=args.retries,
                backoff_s=args.backoff_s,
                sleep_s=args.covers_sleep_s,
            )
            print("Cover download complete.")

        if download_docs_requested:
            print(f"Downloading docs into: {args.docs_dir}")
            manifest = download_docs(
                out_dir=args.docs_dir,
                books=books,
                timeout_s=args.timeout_s,
                retries=args.retries,
                backoff_s=args.backoff_s,
                sleep_s=args.docs_sleep_s,
                insecure_ssl=args.insecure_ssl,
            )
            write_docs_manifest(out_dir=args.docs_dir, manifest=manifest)
            print("Docs download complete.")

            if args.download_docs_with_metadata:
                manifest_path = os.path.join(args.docs_dir, "docs_manifest.json")
                updated = write_pdf_metadata_from_manifest(
                    manifest_path=manifest_path,
                    backup=False,
                    limit=None,
                    write_xmp=True,
                )
                print(f"PDF metadata embedded for {updated} downloaded document(s).")

    if args.write_pdf_metadata:
        manifest_path = args.pdf_metadata_manifest
        if not manifest_path:
            manifest_path = os.path.join(args.docs_dir, "docs_manifest.json")
        if not os.path.exists(manifest_path):
            raise FileNotFoundError(f"Metadata manifest not found: {manifest_path}")
        updated = write_pdf_metadata_from_manifest(
            manifest_path=manifest_path,
            backup=args.pdf_metadata_backup,
            limit=args.pdf_metadata_limit,
            write_xmp=args.pdf_metadata_calibre_web_xmp,
        )
        print(f"PDF metadata updated for {updated} file(s).")


if __name__ == "__main__":
    main()

