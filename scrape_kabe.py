#!/usr/bin/env python3
"""
Scraper for https://kabe.drepram.com/ (Kacabenggala Editions).

- Discovers work URLs from the index (homepage or /works).
- For each /works/<slug>, parses schema.org Book JSON-LD and the first
  /api/documents/file/...pdf link.
"""
import argparse
import csv
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from pdf_metadata_embed import embed_pdf_metadata

BASE_URL = "https://kabe.drepram.com"
INDEX_PATHS = ("/", "/works")
# Embedded PDF / XMP publisher (site: Kacabenggala Editions; imprint name for metadata)
KABE_PUBLISHER = "Kacabenggala"


def _http_get(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-kabe/1.0)"}
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout) as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(backoff_s * (2**attempt) + random.uniform(0, 0.25))
    assert last_err is not None
    raise last_err


def _discover_work_paths(html: str) -> List[str]:
    paths = sorted(set(re.findall(r'href="(/works/[a-z0-9-]+)"', html, flags=re.I)))
    return paths


def _parse_json_ld_blocks(html: str) -> List[Any]:
    out: List[Any] = []
    for m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            out.extend(data)
        else:
            out.append(data)
    return out


def _book_from_json_ld(items: List[Any]) -> Optional[Dict[str, Any]]:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("@type") != "Book":
            continue
        author = item.get("author")
        author_name = ""
        if isinstance(author, dict) and author.get("@type") == "Person":
            author_name = str(author.get("name") or "")
        elif isinstance(author, list) and author:
            names = []
            for a in author:
                if isinstance(a, dict) and a.get("name"):
                    names.append(str(a["name"]))
            author_name = " & ".join(names)
        return {
            "title": str(item.get("name") or ""),
            "author": author_name,
            "year": str(item.get("datePublished") or ""),
            "description": str(item.get("description") or ""),
            "image": item.get("image"),
        }
    return None


def _first_pdf_path(html: str) -> Optional[str]:
    m = re.search(r'(/api/documents/file/[^"\s]+\.pdf)', html, flags=re.IGNORECASE)
    return m.group(1) if m else None


def scrape_work(slug: str, html: str) -> Dict[str, Any]:
    ld = _parse_json_ld_blocks(html)
    book = _book_from_json_ld(ld) or {}
    pdf_rel = _first_pdf_path(html)
    pdf_url = urllib.parse.urljoin(BASE_URL, pdf_rel) if pdf_rel else None
    return {
        "slug": slug,
        "url": f"{BASE_URL}/works/{slug}",
        "title": book.get("title") or "",
        "author": book.get("author") or "",
        "year": book.get("year") or "",
        "description": book.get("description") or "",
        "image": book.get("image"),
        "pdf_path": pdf_rel,
        "pdf_url": pdf_url,
    }


def scrape_site(
    *,
    index_path: str,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if index_path not in INDEX_PATHS:
        raise ValueError(f"--index must be one of {INDEX_PATHS}")
    index_url = urllib.parse.urljoin(BASE_URL, index_path)
    index_html = _http_get(index_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
    work_paths = _discover_work_paths(index_html)
    meta = {
        "base_url": BASE_URL,
        "index_url": index_url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "work_count": len(work_paths),
    }
    books: List[Dict[str, Any]] = []
    for i, path in enumerate(work_paths):
        slug = path.rstrip("/").split("/")[-1]
        work_url = urllib.parse.urljoin(BASE_URL, path)
        html = _http_get(work_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
        row = scrape_work(slug, html)
        row["source_index"] = index_path
        books.append(row)
        if sleep_s > 0 and i + 1 < len(work_paths):
            time.sleep(sleep_s)
    return meta, books


def write_outputs(*, out_dir: str, meta: Dict[str, Any], books: List[Dict[str, Any]]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "books.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "books": books}, f, ensure_ascii=False, indent=2)

    keys = set()
    for b in books:
        keys.update(b.keys())
    preferred = [
        "slug",
        "title",
        "author",
        "year",
        "pdf_url",
        "url",
        "description",
        "image",
        "pdf_path",
    ]
    fieldnames = [k for k in preferred if k in keys] + sorted(keys - set(preferred))
    with open(os.path.join(out_dir, "books.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for b in books:
            w.writerow(b)


def _pdf_subject(book: Dict[str, Any]) -> str:
    d = str(book.get("description") or "").strip()
    if d:
        return d
    year = str(book.get("year") or "").strip()
    if year:
        return f"Kacabenggala Editions · {year}"
    return "Kacabenggala Editions"


def _keywords_from_tags(tags: List[str]) -> str:
    return ", ".join(str(t) for t in tags if t)


def _slugify(s: str, *, max_len: int = 100) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "book"


def _download_stream(url: str, dest: str, *, timeout_s: int, retries: int, backoff_s: float) -> None:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-kabe/1.0)"}
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


def download_pdfs(
    *,
    books: List[Dict[str, Any]],
    files_dir: str,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
    embed_metadata: bool,
    embed_backup: bool,
) -> List[Dict[str, Any]]:
    os.makedirs(files_dir, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    n = len(books)
    for i, b in enumerate(books, start=1):
        slug = str(b.get("slug") or "book")
        pdf_url = b.get("pdf_url")
        title = str(b.get("title") or slug)
        author = str(b.get("author") or "")
        year = str(b.get("year") or "")
        description = str(b.get("description") or "")
        tags = ["Kacabenggala Editions"]
        if year:
            tags.append(year)
        subject = _pdf_subject(b)
        keywords = _keywords_from_tags(tags)

        if not pdf_url:
            manifest.append(
                {
                    "slug": slug,
                    "title": title,
                    "author": author,
                    "year": year,
                    "description": description,
                    "subject": subject,
                    "publisher": KABE_PUBLISHER,
                    "tags": tags,
                    "source_url": None,
                    "file_path": None,
                    "status": "failed",
                    "error": "no pdf_url",
                }
            )
            continue

        safe = _slugify(slug)
        dest = os.path.join(files_dir, f"{safe}.pdf")

        def _embed(dest_path: str) -> None:
            if not embed_metadata:
                return
            try:
                embed_pdf_metadata(
                    dest_path,
                    title=title,
                    author=author,
                    subject=subject,
                    keywords=keywords,
                    publisher=KABE_PUBLISHER,
                    write_xmp=True,
                    backup=embed_backup,
                )
                print(f"[pdf-meta] {slug}")
            except Exception as ex:
                print(f"[pdf-meta] failed {slug}: {ex}", file=sys.stderr)

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            manifest.append(
                {
                    "slug": slug,
                    "title": title,
                    "author": author,
                    "year": year,
                    "description": description,
                    "subject": subject,
                    "publisher": KABE_PUBLISHER,
                    "tags": tags,
                    "source_url": pdf_url,
                    "file_path": dest,
                    "status": "skipped",
                }
            )
            print(f"[pdf] {i}/{n} {slug} -> {dest} (skipped)")
            _embed(dest)
            continue

        try:
            _download_stream(pdf_url, dest, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
            manifest.append(
                {
                    "slug": slug,
                    "title": title,
                    "author": author,
                    "year": year,
                    "description": description,
                    "subject": subject,
                    "publisher": KABE_PUBLISHER,
                    "tags": tags,
                    "source_url": pdf_url,
                    "file_path": dest,
                    "status": "downloaded",
                }
            )
            print(f"[pdf] {i}/{n} {slug} -> {dest}")
            _embed(dest)
        except Exception as e:
            manifest.append(
                {
                    "slug": slug,
                    "title": title,
                    "author": author,
                    "year": year,
                    "description": description,
                    "subject": subject,
                    "publisher": KABE_PUBLISHER,
                    "tags": tags,
                    "source_url": pdf_url,
                    "file_path": None,
                    "status": "failed",
                    "error": str(e),
                }
            )
            print(f"[pdf] failed {i}/{n} {slug}: {e}", file=sys.stderr)

        if sleep_s > 0:
            time.sleep(sleep_s)

    return manifest


def write_manifest(*, files_dir: str, manifest: List[Dict[str, Any]]) -> None:
    os.makedirs(files_dir, exist_ok=True)
    jpath = os.path.join(files_dir, "books_manifest.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    cpath = os.path.join(files_dir, "books_manifest.csv")
    fields = [
        "slug",
        "title",
        "author",
        "year",
        "description",
        "subject",
        "publisher",
        "tags",
        "source_url",
        "file_path",
        "status",
        "error",
    ]
    with open(cpath, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in manifest:
            r = dict(row)
            t = r.get("tags")
            if isinstance(t, list):
                r["tags"] = "|".join(str(x) for x in t if x)
            w.writerow(r)


def embed_metadata_from_manifest(
    *,
    manifest_path: str,
    backup: bool,
    limit: Optional[int],
) -> int:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest, list):
        raise ValueError("manifest must be a JSON array")
    updated = 0
    for row in manifest:
        if limit is not None and updated >= limit:
            break
        if row.get("status") not in ("downloaded", "skipped"):
            continue
        path = row.get("file_path")
        if not path or not str(path).lower().endswith(".pdf"):
            continue
        if not os.path.isfile(path):
            continue
        title = str(row.get("title") or "")
        author = str(row.get("author") or "")
        subject = str(row.get("subject") or "")
        if not subject:
            subject = _pdf_subject(
                {
                    "description": row.get("description"),
                    "year": row.get("year"),
                }
            )
        tags = row.get("tags") or []
        if isinstance(tags, str):
            keywords = tags.replace("|", ", ")
        else:
            keywords = _keywords_from_tags(tags)
        publisher = str(row.get("publisher") or KABE_PUBLISHER)
        try:
            embed_pdf_metadata(
                path,
                title=title,
                author=author,
                subject=subject,
                keywords=keywords,
                publisher=publisher,
                write_xmp=True,
                backup=backup,
            )
            updated += 1
            print(f"[pdf-meta] {path}")
        except Exception as e:
            print(f"[pdf-meta] failed {path}: {e}", file=sys.stderr)
    return updated


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape kabe.drepram.com (Kacabenggala Editions)")
    p.add_argument("--out-dir", default="output/kabe", help="books.json / books.csv")
    p.add_argument("--index", default="/", choices=list(INDEX_PATHS), help="Page to discover /works/* links from")
    p.add_argument("--timeout-s", type=int, default=60)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--backoff-s", type=float, default=0.5)
    p.add_argument("--sleep-s", type=float, default=0.15)

    p.add_argument(
        "--download-pdfs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download missing PDFs into --files-dir (default: enabled). Use --no-download-pdfs to skip.",
    )
    p.add_argument("--files-dir", default="output/kabe/files")
    p.add_argument(
        "--no-embed-pdf-metadata",
        action="store_true",
        help="With --download-pdfs, do not embed title/author/subject/keywords (+XMP) into PDFs",
    )
    p.add_argument(
        "--embed-pdf-metadata-only",
        action="store_true",
        help="Only read books_manifest.json under --files-dir and embed metadata (no scrape/download)",
    )
    p.add_argument("--embed-backup", action="store_true", help="Create .bak before embedding")
    p.add_argument("--embed-limit", type=int, default=None, help="Embed only first N PDFs (testing)")

    args = p.parse_args()

    if args.embed_pdf_metadata_only:
        mp = os.path.join(args.files_dir, "books_manifest.json")
        if not os.path.isfile(mp):
            sys.exit(f"Manifest not found: {mp}")
        n = embed_metadata_from_manifest(manifest_path=mp, backup=args.embed_backup, limit=args.embed_limit)
        print(f"Embedded metadata in {n} PDF(s).")
        return

    meta, books = scrape_site(
        index_path=args.index,
        timeout_s=args.timeout_s,
        retries=args.retries,
        backoff_s=args.backoff_s,
        sleep_s=args.sleep_s,
    )
    print(f"Found {len(books)} works.")
    write_outputs(out_dir=args.out_dir, meta=meta, books=books)
    print(f"Wrote: {os.path.join(args.out_dir, 'books.json')} and books.csv")

    if args.download_pdfs:
        manifest = download_pdfs(
            books=books,
            files_dir=args.files_dir,
            timeout_s=args.timeout_s,
            retries=args.retries,
            backoff_s=args.backoff_s,
            sleep_s=args.sleep_s,
            embed_metadata=not args.no_embed_pdf_metadata,
            embed_backup=args.embed_backup,
        )
        write_manifest(files_dir=args.files_dir, manifest=manifest)
        print(f"Wrote: {os.path.join(args.files_dir, 'books_manifest.json')}")


if __name__ == "__main__":
    main()
