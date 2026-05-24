#!/usr/bin/env python3
"""
Scraper for https://pussinbed.com/ (Puss in Bed).

Uses archive.org's puss-in-bed-library collection as the source of truth for
the complete list of items, then fetches pussinbed.com detail pages for
full metadata where available (falling back to archive.org data on 404).
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

BASE_URL = "https://pussinbed.com"
PRISMIC_CDN = "https://pussinbed.cdn.prismic.io"
PRISMIC_API = PRISMIC_CDN + "/api/v1"
ARCHIVE_COLLECTION = "puss-in-bed-library"
PUSSINBED_PUBLISHER = "Puss in Bed"


def _http_get_json(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> Dict[str, Any]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-pussinbed/1.0)"}
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


def _http_get(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-pussinbed/1.0)"}
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


def _get_master_ref() -> str:
    api_root = _http_get_json(PRISMIC_API, timeout_s=30, retries=3, backoff_s=0.5)
    for ref in api_root.get("refs", []):
        if ref.get("isMasterRef"):
            return ref["ref"]
    raise RuntimeError("Could not find master ref in Prismic API")


def _rich_text_to_string(rt: Any) -> str:
    if isinstance(rt, list):
        return " ".join(_rich_text_to_string(v) for v in rt)
    if isinstance(rt, dict):
        rt_type = rt.get("type", "")
        if rt_type in ("heading1", "heading2", "paragraph"):
            return rt.get("text", "")
        if rt_type == "StructuredText":
            return _rich_text_to_string(rt.get("value", []))
        return ""
    return ""


def _url_slug_from_prismic_slug(slug: str) -> str:
    if "%" in slug:
        slug = urllib.parse.unquote(slug)
    slug = re.sub(r"\.(\d)", r"-\1", slug)
    slug = slug.replace(".-", "-")
    while "---" in slug:
        slug = slug.replace("---", "-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.rstrip(".")
    return slug


def _identifier_to_archive_url(identifier: str) -> str:
    return f"https://archive.org/download/{identifier}/{identifier}.pdf"


def _fetch_archive_items(
    *,
    timeout_s: int,
    retries: int,
    backoff_s: float,
) -> List[Dict[str, Any]]:
    url = (
        f"https://archive.org/advancedsearch.php"
        f"?q=collection:{ARCHIVE_COLLECTION}"
        f"&fl[]=identifier&fl[]=title&fl[]=mediatype"
        f"&sort[]=identifier+asc"
        f"&rows=500&output=json"
    )
    data = _http_get_json(url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
    docs = data.get("response", {}).get("docs", [])
    items = []
    for doc in docs:
        identifier = doc.get("identifier", "")
        title = doc.get("title", "")
        items.append({
            "archive_identifier": identifier,
            "archive_url": _identifier_to_archive_url(identifier),
            "title": title,
        })
    return items


def _safe_prismic_text(raw: Dict[str, Any], key: str) -> str:
    field = raw.get(key)
    if not isinstance(field, dict):
        return ""
    val = field.get("value")
    if isinstance(val, list):
        return " ".join(
            t.get("text", "") for t in val if isinstance(t, dict)
        )
    return str(val) if val else ""


def _fetch_prismic_collections(
    *,
    ref: str,
    timeout_s: int,
    retries: int,
    backoff_s: float,
) -> List[Dict[str, Any]]:
    PAGE_SIZE = 100
    collections: List[Dict[str, Any]] = []

    for page in range(1, 999):
        url = (
            f"{PRISMIC_API}/documents/search"
            f"?ref={ref}"
            f"&query=%5B%5D&type=collection"
            f"&pageSize={PAGE_SIZE}&page={page}&lang=en-us"
        )
        data = _http_get_json(url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
        results = data.get("results", [])
        if not results:
            break

        for doc in results:
            raw = doc.get("data", {}).get("collection", {})
            collection_title_raw = raw.get("collection_title", {})

            has_title = False
            if isinstance(collection_title_raw, dict):
                val = collection_title_raw.get("value", [])
                if isinstance(val, list):
                    has_title = any(
                        (isinstance(x, dict) and x.get("text")) for x in val
                    )
                elif val:
                    has_title = bool(val)
            elif isinstance(collection_title_raw, list):
                has_title = any(
                    (isinstance(x, dict) and x.get("text")) for x in collection_title_raw
                )
            elif collection_title_raw:
                has_title = True

            if not has_title:
                continue

            slugs = doc.get("slugs", [])
            slug = slugs[0] if slugs else ""
            if not slug:
                continue

            url_slug = _url_slug_from_prismic_slug(slug)

            author_field = raw.get("collection_author")
            author = ""
            if isinstance(author_field, dict):
                author = author_field.get("value", "")

            pages_field = raw.get("collection_pages")
            pages = ""
            if isinstance(pages_field, dict):
                pages = str(pages_field.get("value", ""))

            size_field = raw.get("collection_size")
            size = ""
            if isinstance(size_field, dict):
                size = str(size_field.get("value", ""))

            language_raw = raw.get("collection_language")
            language = ""
            if isinstance(language_raw, dict):
                lang_list = language_raw.get("value", [])
                if isinstance(lang_list, list) and lang_list:
                    lang_item = lang_list[0]
                    if isinstance(lang_item, dict):
                        lang_inner = lang_item.get("language", {})
                        if isinstance(lang_inner, dict):
                            language = lang_inner.get("value", "")

            year_field = raw.get("collection_publication_year")
            year = ""
            if isinstance(year_field, dict):
                year_val = year_field.get("value")
                if year_val:
                    year = str(int(year_val))

            description = _safe_prismic_text(raw, "collection_brief_paragraph")

            notes = _safe_prismic_text(raw, "collection_notes")

            origin = _safe_prismic_text(raw, "collection_origin")

            print_method = _safe_prismic_text(raw, "collection_print_method")

            entry_number = _safe_prismic_text(raw, "collection_entry_number")

            collections.append({
                "slug": slug,
                "url": f"{BASE_URL}/collection/zine/{url_slug}/",
                "author": author,
                "pages": pages,
                "size": size,
                "language": language,
                "year": year,
                "description": description,
                "notes": notes,
                "origin": origin,
                "print_method": print_method,
                "entry_number": entry_number,
            })

        if len(results) < PAGE_SIZE:
            break

    return collections


def _slugify_archive_identifier(identifier: str) -> str:
    identifier = identifier.strip().lower()
    return identifier


def _try_prismic_slug_from_archive(archive_identifier: str) -> Optional[str]:
    parts = archive_identifier.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        base = parts[0]
        num = parts[1]
        candidates = [
            f"{base}.{num}",
            f"{base}-{num}",
            f"{base}#{num}",
        ]
        return base, num, candidates
    return None


def scrape_work(url: str, html: str) -> Dict[str, Any]:
    slug = urllib.parse.urlparse(url).path.strip("/").split("/")[-1]

    def extract(pattern: str, flags: int = re.IGNORECASE) -> str:
        m = re.search(pattern, html, flags=flags)
        return m.group(1).strip() if m else ""

    entry_num = extract(r'Entry Number\s*</[^>]*>\s*<[^>]*>\s*([^<]+)')

    author = ""
    author_match = re.search(r'<h2[^>]*>\s*([^<]+)\s*</h2>', html)
    if author_match:
        raw_author = author_match.group(1).strip()
        if raw_author and raw_author not in ("External Links", "Notes"):
            author = raw_author

    if not author and description:
        author_m = re.search(r"\bby\s+([^,]+(?:\s+[^,]+)*?)(?:[,\.]|$)", description, re.IGNORECASE)
        if author_m:
            author = author_m.group(1).strip()

    year = extract(r'Publication date\s*</[^>]*>\s*<[^>]*>\s*<a[^>]*>([^<]+)')
    if not year:
        year = extract(r'/years/(\d{4})/')

    language = extract(r'Language\s*</[^>]*>\s*<[^>]*>\s*([^<]+)')
    pages = extract(r'(\d+)\s*pages?', re.IGNORECASE)
    size = extract(r'Size\s*</[^>]*>\s*<[^>]*>\s*([^<]+)')
    print_method = extract(r'Print Method\s*</[^>]*>\s*<[^>]*>\s*([^<]+)')
    origin = extract(r'Origin\s*</[^>]*>\s*<[^>]*>\s*([^<]+)')

    topics = re.findall(r'/topics/([^"/]+)/"', html)

    archive_link_match = re.search(r'href="(https://archive\.org/download/[^"]+\.pdf)"', html, flags=re.IGNORECASE)
    archive_link = archive_link_match.group(1) if archive_link_match else None

    title = extract(r'<h1[^>]*>\s*([^<]+)\s*</h1>')
    if not title:
        title = extract(r'<title>([^<]+)</title>')

    description_blocks = re.findall(r'<p[^>]*>([^<]+)</p>', html)
    description = ""
    for block in description_blocks:
        if len(block) > 50:
            description = block
            break

    return {
        "slug": slug,
        "url": url,
        "title": title,
        "author": author,
        "year": year,
        "language": language,
        "pages": pages,
        "size": size,
        "print_method": print_method,
        "origin": origin,
        "topics": topics,
        "entry_number": entry_num,
        "archive_url": archive_link,
        "description": description,
    }


def scrape_site(
    *,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    print("Fetching item list from archive.org...")
    archive_items = _fetch_archive_items(
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
    )
    print(f"Found {len(archive_items)} items in archive.org collection")

    print("Fetching Prismic master ref...")
    ref = _get_master_ref()

    print("Fetching Prismic collection data...")
    prismic_collections = _fetch_prismic_collections(
        ref=ref,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
    )
    prismic_by_slug = {c["slug"]: c for c in prismic_collections}
    print(f"Fetched {len(prismic_collections)} Prismic collections")

    meta = {
        "base_url": BASE_URL,
        "archive_collection": ARCHIVE_COLLECTION,
        "prismic_api": PRISMIC_API,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "work_count": len(archive_items),
    }

    books: List[Dict[str, Any]] = []
    for i, item in enumerate(archive_items, start=1):
        archive_id = item["archive_identifier"]
        archive_url = item["archive_url"]
        archive_title = item["title"]

        pussinbed_data = None
        prismic_slug_used = None

        for prismic_slug, pussinbed_candidate in prismic_by_slug.items():
            if _urls_match_prismic(archive_id, prismic_slug):
                pussinbed_data = pussinbed_candidate
                prismic_slug_used = prismic_slug
                break

        if pussinbed_data:
            row = _row_from_archive_item(item)
            row["prismic_slug"] = prismic_slug_used
            row["pussinbed_status"] = "prismic"
            row["url"] = pussinbed_data["url"]
            row["author"] = pussinbed_data.get("author", "")
            row["pages"] = pussinbed_data.get("pages", "")
            row["size"] = pussinbed_data.get("size", "")
            row["language"] = pussinbed_data.get("language", "")
            row["year"] = pussinbed_data.get("year", "")
            row["description"] = pussinbed_data.get("description", "")
            row["notes"] = pussinbed_data.get("notes", "")
            row["origin"] = pussinbed_data.get("origin", "")
            row["print_method"] = pussinbed_data.get("print_method", "")
            row["entry_number"] = pussinbed_data.get("entry_number", "")
            books.append(row)
            print(f"[{i}/{len(archive_items)}] OK   {archive_id} (via Prismic)")
        else:
            row = _row_from_archive_item(item)
            row["pussinbed_status"] = "not_in_prismic"
            books.append(row)
            print(f"[{i}/{len(archive_items)}] DIRECT {archive_id} (not in Prismic, used archive.org)")

        if sleep_s > 0 and i < len(archive_items):
            time.sleep(sleep_s)

    return meta, books


def _urls_match_prismic(archive_identifier: str, prismic_slug: str) -> bool:
    archive_lower = archive_identifier.lower().replace("_", "-").replace(" ", "-")
    prismic_lower = prismic_slug.lower()

    if archive_lower == prismic_lower:
        return True
    if archive_lower.replace(".", "-") == prismic_lower:
        return True
    if archive_lower.replace(".", "-").replace("--", "-") == prismic_lower:
        return True

    arch_base = re.sub(r"-\d{4}$", "", archive_lower)
    if arch_base == prismic_lower:
        return True
    if arch_base.replace(".", "-") == prismic_lower:
        return True

    return False


def _row_from_archive_item(item: Dict[str, Any]) -> Dict[str, Any]:
    identifier = item["archive_identifier"]
    title = item.get("title", identifier)
    year_m = re.search(r"\((\d{4})\)$", title)
    year = year_m.group(1) if year_m else ""
    clean_title = re.sub(r"\s*\(?\d{4}\)?\s*$", "", title).strip()

    return {
        "slug": identifier,
        "url": f"https://pussinbed.com/collection/zine/{identifier}/",
        "title": clean_title,
        "author": "",
        "year": year,
        "language": "",
        "pages": "",
        "size": "",
        "print_method": "",
        "origin": "",
        "topics": [],
        "entry_number": "",
        "archive_url": item.get("archive_url", _identifier_to_archive_url(identifier)),
        "description": "",
        "notes": "",
        "prismic_slug": None,
        "pussinbed_status": "unknown",
    }


def write_outputs(
    *,
    out_dir: str,
    meta: Dict[str, Any],
    books: List[Dict[str, Any]],
) -> None:
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
        "language",
        "pages",
        "entry_number",
        "archive_url",
        "url",
        "description",
        "notes",
        "topics",
        "size",
        "print_method",
        "origin",
        "pussinbed_status",
        "prismic_slug",
    ]
    fieldnames = [k for k in preferred if k in keys] + sorted(keys - set(preferred))
    with open(os.path.join(out_dir, "books.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for b in books:
            w.writerow(b)


def _slugify(s: str, *, max_len: int = 100) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "book"


def _discover_archive_pdf_url(identifier: str, *, timeout_s: int, retries: int, backoff_s: float) -> Optional[str]:
    url = f"https://archive.org/download/{identifier}/{identifier}.pdf"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-pussinbed/1.0)"}
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if resp.status == 200:
                    return url
                break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            last_err = e
            if attempt >= retries:
                break
            time.sleep(backoff_s * (2**attempt) + random.uniform(0, 0.25))
        except (urllib.error.URLError, socket.timeout) as e:
            last_err = e
            if attempt >= retries:
                break
            time.sleep(backoff_s * (2**attempt) + random.uniform(0, 0.25))

    list_url = f"https://archive.org/download/{identifier}/"
    try:
        html = _http_get(list_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
        pdfs = re.findall(r'href="([^"]+\.pdf)"', html)
        if pdfs:
            best = pdfs[0]
            for p in pdfs:
                if identifier.replace("_", "-").replace(" ", "-").lower() in p.lower().replace("_", "-").replace(" ", "-"):
                    best = p
                    break
            if not best.startswith("http"):
                best = f"https://archive.org/download/{identifier}/{best}"
            return best
    except Exception:
        pass

    if last_err:
        raise last_err
    return None


def _download_stream(url: str, dest: str, *, timeout_s: int, retries: int, backoff_s: float) -> None:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-pussinbed/1.0)"}
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
        archive_url = b.get("archive_url")

        title = str(b.get("title") or slug)
        author = str(b.get("author") or "")
        year = str(b.get("year") or "")
        topics = b.get("topics", [])
        description = str(b.get("description") or "")
        entry_num = str(b.get("entry_number") or "")

        if description:
            subject = description
        elif year:
            subject = f"{PUSSINBED_PUBLISHER} · {year}"
        else:
            subject = PUSSINBED_PUBLISHER

        keywords = ", ".join(str(t) for t in topics if t)

        def _embed(dest_path: str) -> None:
            if not embed_metadata:
                return
            if not str(dest_path).lower().endswith(".pdf"):
                return
            try:
                embed_pdf_metadata(
                    dest_path,
                    title=title,
                    author=author,
                    subject=subject,
                    keywords=keywords,
                    publisher=PUSSINBED_PUBLISHER,
                    write_xmp=True,
                    backup=embed_backup,
                )
                print(f"[pdf-meta] {slug}")
            except Exception as ex:
                print(f"[pdf-meta] failed {slug}: {ex}", file=sys.stderr)

        if not archive_url:
            archive_url = _discover_archive_pdf_url(
                slug, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s
            )

        if not archive_url:
            manifest.append({
                "slug": slug,
                "title": title,
                "author": author,
                "year": year,
                "language": b.get("language", ""),
                "pages": b.get("pages", ""),
                "entry_number": entry_num,
                "topics": topics,
                "description": description,
                "publisher": PUSSINBED_PUBLISHER,
                "source_url": None,
                "file_path": None,
                "status": "failed",
                "error": "no archive_url",
            })
            continue

        safe = _slugify(slug)
        dest = os.path.join(files_dir, f"{safe}.pdf")

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            manifest.append({
                "slug": slug,
                "title": title,
                "author": author,
                "year": year,
                "language": b.get("language", ""),
                "pages": b.get("pages", ""),
                "entry_number": entry_num,
                "topics": topics,
                "description": description,
                "publisher": PUSSINBED_PUBLISHER,
                "source_url": archive_url,
                "file_path": dest,
                "status": "skipped",
            })
            print(f"[pdf] {i}/{n} {slug} -> {dest} (skipped)")
            _embed(dest)
            continue

        try:
            _download_stream(archive_url, dest, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
            manifest.append({
                "slug": slug,
                "title": title,
                "author": author,
                "year": year,
                "language": b.get("language", ""),
                "pages": b.get("pages", ""),
                "entry_number": entry_num,
                "topics": topics,
                "description": description,
                "publisher": PUSSINBED_PUBLISHER,
                "source_url": archive_url,
                "file_path": dest,
                "status": "downloaded",
            })
            print(f"[pdf] {i}/{n} {slug} -> {dest}")
            _embed(dest)
        except Exception as e:
            discovered_url = None
            if "404" in str(e) or "Not Found" in str(e):
                discovered_url = _discover_archive_pdf_url(
                    slug, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s
                )

            if discovered_url and discovered_url != archive_url:
                try:
                    _download_stream(discovered_url, dest, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
                    manifest.append({
                        "slug": slug,
                        "title": title,
                        "author": author,
                        "year": year,
                        "language": b.get("language", ""),
                        "pages": b.get("pages", ""),
                        "entry_number": entry_num,
                        "topics": topics,
                        "description": description,
                        "publisher": PUSSINBED_PUBLISHER,
                        "source_url": discovered_url,
                        "file_path": dest,
                        "status": "downloaded",
                    })
                    print(f"[pdf] {i}/{n} {slug} -> {dest} (discovered)")
                    _embed(dest)
                    continue
                except Exception as e2:
                    e = e2

            manifest.append({
                "slug": slug,
                "title": title,
                "author": author,
                "year": year,
                "language": b.get("language", ""),
                "pages": b.get("pages", ""),
                "entry_number": entry_num,
                "topics": topics,
                "description": description,
                "publisher": PUSSINBED_PUBLISHER,
                "source_url": archive_url,
                "file_path": None,
                "status": "failed",
                "error": str(e),
            })
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
        "slug", "title", "author", "year", "language", "pages", "entry_number",
        "topics", "description", "publisher", "source_url", "file_path", "status", "error",
    ]
    with open(cpath, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in manifest:
            r = dict(row)
            t = r.get("topics")
            if isinstance(t, list):
                r["topics"] = "|".join(str(x) for x in t if x)
            w.writerow(r)


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape pussinbed.com")
    p.add_argument("--out-dir", default="output/pussinbed", help="books.json / books.csv")
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
    p.add_argument("--files-dir", default="output/pussinbed/files")
    p.add_argument("--pdfs-sleep-s", type=float, default=0.1)
    p.add_argument(
        "--no-embed-pdf-metadata",
        action="store_true",
        help="Do not embed title/author/subject/keywords (+XMP) into downloaded PDFs",
    )
    p.add_argument("--embed-backup", action="store_true", help="Create .bak before embedding")

    args = p.parse_args()

    meta, books = scrape_site(
        timeout_s=args.timeout_s,
        retries=args.retries,
        backoff_s=args.backoff_s,
        sleep_s=args.sleep_s,
    )

    print(f"Total: {len(books)} works.")
    write_outputs(out_dir=args.out_dir, meta=meta, books=books)
    print(f"Wrote: {os.path.join(args.out_dir, 'books.json')} and books.csv")

    if args.download_pdfs:
        manifest = download_pdfs(
            books=books,
            files_dir=args.files_dir,
            timeout_s=args.timeout_s,
            retries=args.retries,
            backoff_s=args.backoff_s,
            sleep_s=args.pdfs_sleep_s,
            embed_metadata=not args.no_embed_pdf_metadata,
            embed_backup=args.embed_backup,
        )
        write_manifest(files_dir=args.files_dir, manifest=manifest)
        print(f"Wrote: {os.path.join(args.files_dir, 'books_manifest.json')}")


if __name__ == "__main__":
    main()