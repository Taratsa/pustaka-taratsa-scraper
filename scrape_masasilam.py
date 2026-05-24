#!/usr/bin/env python3
"""
Scraper for https://masasilam.com/api/books (MasasilaM).

API shape:
  { "result": "Success", "code": 200, "data": { "page", "limit", "total", "list": [ ... ] } }

Pagination: ?page=1&limit=12
"""
import argparse
import csv
import json
import math
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BASE_URL = "https://masasilam.com/api/books"
DEFAULT_LIMIT = 50


def _build_url(page: int, limit: int) -> str:
    return f"{BASE_URL}?page={page}&limit={limit}"


def _http_get_json(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> Dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; scrape-masasilam/1.0)",
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
            time.sleep(backoff_s * (2**attempt) + random.uniform(0, 0.25))
    assert last_err is not None
    raise last_err


def _slugify(s: str, *, max_len: int = 80) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "book"


def _tags_from_book(b: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    cat = b.get("category")
    if cat:
        tags.append(str(cat))
    genres = b.get("genres")
    if genres and isinstance(genres, str):
        for part in re.split(r"\s*,\s*", genres):
            if part and part not in tags:
                tags.append(part)
    return tags


def scrape_books(
    *,
    limit: int,
    start_page: int,
    end_page: Optional[int],
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    first_url = _build_url(start_page, limit)
    first = _http_get_json(first_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)

    if first.get("code") != 200 and first.get("result") != "Success":
        raise RuntimeError(f"API error: {first}")

    data = first.get("data") or {}
    total = int(data.get("total", 0))
    total_pages = max(1, math.ceil(total / limit)) if total else 1

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
        "limit": limit,
        "total": total,
        "totalPages": total_pages,
        "start_page": start_page,
        "end_page": end_page,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    for page in range(start_page, end_page + 1):
        url = _build_url(page, limit)
        payload = first if page == start_page else _http_get_json(url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
        d = payload.get("data") or {}
        page_list = d.get("list") or []
        for item in page_list:
            row = dict(item)
            row["source_page"] = page
            books.append(row)
        if sleep_s > 0 and page != end_page:
            time.sleep(sleep_s)

    return meta, books


def write_outputs(*, out_dir: str, meta: Dict[str, Any], books: List[Dict[str, Any]]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "books.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "books": books}, f, ensure_ascii=False, indent=2)

    fieldnames_set = set()
    for b in books:
        fieldnames_set.update(b.keys())
    preferred = [
        "id",
        "title",
        "slug",
        "authorNames",
        "category",
        "genres",
        "publisher",
        "publicationYear",
        "fileUrl",
        "fileFormat",
        "coverImageUrl",
        "description",
        "language",
        "publishedAt",
        "source",
        "source_page",
    ]
    fieldnames = [k for k in preferred if k in fieldnames_set] + sorted(fieldnames_set - set(preferred))

    csv_path = os.path.join(out_dir, "books.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for b in books:
            w.writerow(b)


def _download_stream_to_path(url: str, dest: str, *, timeout_s: int, retries: int, backoff_s: float) -> None:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-masasilam/1.0)"}
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                tmp = dest + ".part"
                with open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 128)
                        if not chunk:
                            break
                        out.write(chunk)
                if os.path.getsize(tmp) == 0:
                    os.remove(tmp)
                    raise RuntimeError("downloaded file is 0 bytes")
                os.replace(tmp, dest)
                return
        except Exception as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(backoff_s * (2**attempt) + random.uniform(0, 0.25))
        finally:
            tmp = dest + ".part"
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    assert last_err is not None
    raise last_err


def download_books(
    *,
    books_dir: str,
    books: List[Dict[str, Any]],
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> List[Dict[str, Any]]:
    os.makedirs(books_dir, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    total = len(books)

    for i, b in enumerate(books, start=1):
        file_url = b.get("fileUrl")
        if not file_url:
            manifest.append(
                {
                    "book_id": b.get("id"),
                    "title": str(b.get("title") or ""),
                    "author": str(b.get("authorNames") or ""),
                    "category": str(b.get("category") or ""),
                    "tags": _tags_from_book(b),
                    "source_url": None,
                    "file_path": None,
                    "file_format": b.get("fileFormat"),
                    "status": "failed",
                    "error": "no fileUrl",
                }
            )
            continue

        bid = b.get("id")
        slug = str(b.get("slug") or _slugify(str(b.get("title") or "book")))
        fmt = (b.get("fileFormat") or "bin")
        if isinstance(fmt, str):
            fmt = fmt.lower().lstrip(".")
        else:
            fmt = "bin"

        prefix = f"{bid}-{slug}"
        dest = os.path.join(books_dir, f"{prefix}.{fmt}")

        title = str(b.get("title") or "")
        author = str(b.get("authorNames") or "")
        category = str(b.get("category") or "")
        tags = _tags_from_book(b)

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            manifest.append(
                {
                    "book_id": bid,
                    "title": title,
                    "author": author,
                    "category": category,
                    "tags": tags,
                    "source_url": str(file_url),
                    "file_path": dest,
                    "file_format": fmt,
                    "status": "skipped",
                }
            )
            print(f"[books] {i}/{total} id={bid} -> {dest} (skipped)")
            continue

        try:
            _download_stream_to_path(str(file_url), dest, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
            manifest.append(
                {
                    "book_id": bid,
                    "title": title,
                    "author": author,
                    "category": category,
                    "tags": tags,
                    "source_url": str(file_url),
                    "file_path": dest,
                    "file_format": fmt,
                    "status": "downloaded",
                }
            )
            print(f"[books] {i}/{total} id={bid} -> {dest}")
        except Exception as e:
            manifest.append(
                {
                    "book_id": bid,
                    "title": title,
                    "author": author,
                    "category": category,
                    "tags": tags,
                    "source_url": str(file_url),
                    "file_path": None,
                    "file_format": fmt,
                    "status": "failed",
                    "error": str(e),
                }
            )
            print(f"[books] failed {i}/{total} id={bid}: {e}", file=sys.stderr)

        if sleep_s > 0:
            time.sleep(sleep_s)

    return manifest


def write_books_manifest(*, books_dir: str, manifest: List[Dict[str, Any]]) -> None:
    os.makedirs(books_dir, exist_ok=True)
    jpath = os.path.join(books_dir, "books_manifest.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    cpath = os.path.join(books_dir, "books_manifest.csv")
    fields = ["book_id", "title", "author", "category", "tags", "source_url", "file_path", "file_format", "status", "error"]
    with open(cpath, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in manifest:
            r = dict(row)
            t = r.get("tags")
            if isinstance(t, list):
                r["tags"] = "|".join(str(x) for x in t if x)
            w.writerow(r)


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape masasilam.com /api/books")
    p.add_argument("--out-dir", default="output/masasilam", help="Directory for books.json / books.csv")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Page size (API limit param)")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--end-page", type=int, default=None)

    p.add_argument("--timeout-s", type=int, default=60)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--backoff-s", type=float, default=0.5)
    p.add_argument("--sleep-s", type=float, default=0.1)

    p.add_argument(
        "--download-books",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download missing files from each book fileUrl into --books-dir (default: enabled). Use --no-download-books to skip.",
    )
    p.add_argument("--books-dir", default="output/masasilam/files", help="Where downloaded files go")
    p.add_argument("--books-sleep-s", type=float, default=0.05)

    args = p.parse_args()

    meta, books = scrape_books(
        limit=args.limit,
        start_page=args.start_page,
        end_page=args.end_page,
        timeout_s=args.timeout_s,
        retries=args.retries,
        backoff_s=args.backoff_s,
        sleep_s=args.sleep_s,
    )

    print(f"Fetched {len(books)} books (total={meta['total']}, pages={meta['totalPages']}).")
    write_outputs(out_dir=args.out_dir, meta=meta, books=books)
    print(f"Wrote: {os.path.join(args.out_dir, 'books.json')} and books.csv")

    if args.download_books:
        print(f"Downloading into: {args.books_dir}")
        manifest = download_books(
            books_dir=args.books_dir,
            books=books,
            timeout_s=args.timeout_s,
            retries=args.retries,
            backoff_s=args.backoff_s,
            sleep_s=args.books_sleep_s,
        )
        write_books_manifest(books_dir=args.books_dir, manifest=manifest)
        print(f"Wrote manifest: {os.path.join(args.books_dir, 'books_manifest.json')}")


if __name__ == "__main__":
    main()
