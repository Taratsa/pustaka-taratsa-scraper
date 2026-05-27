#!/usr/bin/env python3
"""
Scraper for https://repositori.kemendikdasmen.go.id/view/subjects/<subject>

Browses subject pages and downloads PDFs from the Kemdikdasmen repository.
Uses EPrint meta tags for comprehensive metadata extraction.
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

BASE_URL = "https://repositori.kemendikdasmen.go.id"
PUBLISHER = "Perpustakaan Kemendikdasmen"


def _find_subject_by_name(name: str, timeout_s: int, retries: int, backoff_s: float) -> Optional[Dict[str, str]]:
    subjects_html = _http_get(
        urllib.parse.urljoin(BASE_URL, "/view/subjects/"),
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
    )
    subjects = _discover_subjects(subjects_html)
    name_lower = name.lower().strip()
    path_lower = _slugify(name_lower)
    for s in subjects:
        if _slugify(s["name"]).replace("-", "") == path_lower.replace("-", ""):
            return s
        if _slugify(s["path"].split("/")[-1].replace(".html", "")).replace("-", "") == path_lower.replace("-", ""):
            return s
    return None


def _find_subject_by_path(path: str, timeout_s: int, retries: int, backoff_s: float) -> Optional[Dict[str, str]]:
    subjects_html = _http_get(
        urllib.parse.urljoin(BASE_URL, "/view/subjects/"),
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
    )
    subjects = _discover_subjects(subjects_html)
    path_slug = _slugify(path.split("/")[-1].replace(".html", ""))
    for s in subjects:
        if _slugify(s["path"].split("/")[-1].replace(".html", "")) == path_slug:
            return s
    return None


def _http_get(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-repositori/1.0)"}
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
            print(f"[retry] {url} — attempt {attempt+1}/{retries+1} failed: {e}")
            time.sleep(backoff_s * (2**attempt) + random.uniform(0, 0.25))
    assert last_err is not None
    raise last_err


def _parse_eprint_meta(html: str) -> Dict[str, Any]:
    meta_dict: Dict[str, Any] = {}
    for m in re.finditer(
        r'<meta name="eprints\.([^"]+)" content="([^"]*)"',
        html,
    ):
        name = m.group(1)
        content = m.group(2)
        if name in meta_dict:
            existing = meta_dict[name]
            if isinstance(existing, list):
                existing.append(content)
            else:
                meta_dict[name] = [existing, content]
        else:
            meta_dict[name] = content
    # Also parse Dublin Core (DC.*) meta tags — DC.date is the canonical date field
    for m in re.finditer(
        r'<meta name="(DC\.[^"]+)" content="([^"]*)"',
        html,
    ):
        name = m.group(1)
        content = m.group(2)
        if name in meta_dict:
            existing = meta_dict[name]
            if isinstance(existing, list):
                existing.append(content)
            else:
                meta_dict[name] = [existing, content]
        else:
            meta_dict[name] = content
    return meta_dict


def _discover_item_links(html: str, subject_path: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for m in re.finditer(r'repositori\.kemendikdasmen\.go\.id/(\d+)/', html):
        item_id = m.group(1)
        item_url = f"https://repositori.kemendikdasmen.go.id/{item_id}/"
        items.append({"id": item_id, "url": item_url})
    seen = set()
    unique = []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique


def _parse_subject_path(path: str) -> Tuple[str, Optional[str]]:
    href = path.split("/")[-1].replace(".html", "")
    if "=" not in href:
        return href, None
    parts = href.split("=")
    parent_parts = parts[:-1]
    parent_href = "/".join(parent_parts).replace("/", "=2E") if len(parent_parts) > 1 else parts[0]
    parent_path = f"/view/subjects/{parent_href}.html" if parent_href else None
    return href, parent_path


def _discover_subjects(html: str) -> List[Dict[str, Any]]:
    subjects: List[Dict[str, Any]] = []
    for m in re.findall(r'<a[^>]+href="(PED[^"]*\.html)"[^>]*>([^<]+)</a>', html, re.IGNORECASE):
        href = m[0]
        name = m[1].strip()
        subject_path = f"/view/subjects/{href}"
        slug, parent_path = _parse_subject_path(subject_path)
        count_m = re.search(r'\((\d+)\)', m[1])
        count = int(count_m.group(1)) if count_m else 0
        subjects.append({
            "path": subject_path,
            "name": name,
            "slug": slug,
            "parent_path": parent_path,
            "count": count,
        })
    return subjects


def _extract_pdf_url(html: str) -> Optional[str]:
    m = re.search(r'<meta name="eprints\.document_url" content="([^"]*)"', html)
    if m:
        return m.group(1)
    pdf_urls = re.findall(r'https://repositori\.kemendikdasmen\.go\.id/\d+/\d/[^"\s]+\.pdf', html)
    if pdf_urls:
        return pdf_urls[0]
    return None


def _parse_creators(meta: Dict[str, Any]) -> str:
    creators = meta.get("creators_name", [])
    if isinstance(creators, list):
        return "; ".join(creators)
    return str(creators)


def _parse_corp_creators(meta: Dict[str, Any]) -> str:
    corp = meta.get("corp_creators", [])
    if isinstance(corp, list):
        return "; ".join(corp)
    return str(corp) if corp else ""


def _parse_subjects(meta: Dict[str, Any]) -> List[str]:
    subjects = meta.get("subjects", [])
    if isinstance(subjects, list):
        return subjects
    return [subjects] if subjects else []


def _parse_date(meta: Dict[str, Any]) -> Tuple[str, str]:
    # Prefer DC.date (Dublin Core canonical date), fall back to eprints.date
    date = str(meta.get("DC.date", "") or meta.get("date", ""))
    date_type = str(meta.get("date_type", ""))
    year = ""
    if date:
        m = re.search(r'(\d{4})', date)
        if m:
            year = m.group(1)
    return year, date


def scrape_item(item_url: str, item_id: str, html: str) -> Dict[str, Any]:
    meta = _parse_eprint_meta(html)
    title = str(meta.get("title", ""))
    author = _parse_creators(meta)
    corp_creator = _parse_corp_creators(meta)
    abstract = str(meta.get("abstract", ""))
    year, date = _parse_date(meta)
    publisher = str(meta.get("publisher", ""))
    isbn = str(meta.get("isbn", ""))
    volume = str(meta.get("volume", ""))
    pages = str(meta.get("pages", ""))
    item_type = str(meta.get("type", ""))
    editors = meta.get("editors_name", [])
    if isinstance(editors, list):
        editors = "; ".join(editors)
    else:
        editors = str(editors)
    subjects = _parse_subjects(meta)
    pdf_url = _extract_pdf_url(html)

    return {
        "id": item_id,
        "url": item_url,
        "title": title,
        "author": author,
        "corp_creator": corp_creator,
        "year": year,
        "date": date,
        "abstract": abstract,
        "publisher": publisher,
        "isbn": isbn,
        "volume": volume,
        "pages": pages,
        "type": item_type,
        "editors": editors,
        "subjects": subjects,
        "pdf_url": pdf_url,
    }


def scrape_subject_page(
    subject_path: str,
    *,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
    files_dir: Optional[str] = None,
    embed_metadata: bool = True,
    embed_backup: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not subject_path.startswith('/'):
        subject_path = '/' + subject_path
    subject_url = urllib.parse.urljoin(BASE_URL, subject_path)
    index_html = _http_get(subject_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
    items = _discover_item_links(index_html, subject_path)

    meta = {
        "base_url": BASE_URL,
        "subject_url": subject_url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "item_count": len(items),
    }

    manifest: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        print(f"[{subject_path}] Fetching item {i+1}/{len(items)}: {item['url']}")
        item_html = _http_get(item["url"], timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
        row = scrape_item(item["url"], item["id"], item_html)
        row["source_subject"] = subject_path
        results.append(row)

        # Immediately download the PDF for this item
        item_id = str(row.get("id") or item["id"])
        pdf_url = row.get("pdf_url")
        title = str(row.get("title") or item_id)
        author = str(row.get("author") or "")
        year = str(row.get("year") or "")
        date = str(row.get("date") or year)
        abstract = str(row.get("abstract") or "")
        publisher = str(row.get("publisher") or PUBLISHER)
        isbn = str(row.get("isbn") or "")
        item_type = str(row.get("type") or "")
        subjects_list = row.get("subjects", [])
        if isinstance(subjects_list, list):
            subjects_str = ", ".join(str(s) for s in subjects_list if s)
        else:
            subjects_str = str(subjects_list)

        if not pdf_url:
            manifest.append({
                "id": item_id, "title": title, "author": author, "year": year,
                "date": date, "publisher": publisher, "isbn": isbn, "type": item_type,
                "subjects": subjects_str, "abstract": abstract,
                "source_url": None, "file_path": None, "status": "failed", "error": "no pdf_url",
            })
            print(f"[{subject_path}] No PDF URL for {i+1}/{len(items)}")
        else:
            slug = _slugify(title) if title and title != item_id else item_id
            if files_dir:
                os.makedirs(files_dir, exist_ok=True)
                dest = os.path.join(files_dir, f"{slug}.pdf")
            else:
                dest = os.path.join("output/kemendikdasmen/files", f"{slug}.pdf")

            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                manifest.append({
                    "id": item_id, "title": title, "author": author, "year": year,
                    "date": date, "publisher": publisher, "isbn": isbn, "type": item_type,
                    "subjects": subjects_str, "abstract": abstract,
                    "source_url": pdf_url, "file_path": dest, "status": "skipped",
                })
                print(f"[{subject_path}] PDF skipped {i+1}/{len(items)}: {dest}")
            else:
                try:
                    _download_stream(pdf_url, dest, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
                    manifest.append({
                        "id": item_id, "title": title, "author": author, "year": year,
                        "date": date, "publisher": publisher, "isbn": isbn, "type": item_type,
                        "subjects": subjects_str, "abstract": abstract,
                        "source_url": pdf_url, "file_path": dest, "status": "downloaded",
                    })
                    print(f"[{subject_path}] Downloaded {i+1}/{len(items)}: {dest}")
                    if embed_metadata:
                        try:
                            embed_pdf_metadata(
                                dest, title=title, author=author, subject=subjects_str or PUBLISHER,
                                keywords=subjects_str, publisher=publisher or PUBLISHER,
                                publication_date=date or year, write_xmp=True, backup=embed_backup,
                            )
                            print(f"[{subject_path}] Metadata embedded {i+1}/{len(items)}")
                        except Exception as ex:
                            print(f"[{subject_path}] Metadata embed failed {i+1}/{len(items)}: {ex}", file=sys.stderr)
                except Exception as e:
                    manifest.append({
                        "id": item_id, "title": title, "author": author, "year": year,
                        "date": date, "publisher": publisher, "isbn": isbn, "type": item_type,
                        "subjects": subjects_str, "abstract": abstract,
                        "source_url": pdf_url, "file_path": None, "status": "failed", "error": str(e),
                    })
                    print(f"[{subject_path}] Download failed {i+1}/{len(items)}: {e}", file=sys.stderr)

        if sleep_s > 0 and i + 1 < len(items):
            time.sleep(sleep_s)

    return meta, results, manifest


def write_outputs(*, out_dir: str, meta: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "items.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "items": items}, f, ensure_ascii=False, indent=2)

    keys = set()
    for item in items:
        keys.update(item.keys())
    preferred = [
        "id", "title", "author", "corp_creator", "year", "date",
        "publisher", "isbn", "volume", "pages", "type", "editors",
        "pdf_url", "url", "abstract", "subjects",
    ]
    fieldnames = [k for k in preferred if k in keys] + sorted(keys - set(preferred))
    with open(os.path.join(out_dir, "items.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for item in items:
            row = dict(item)
            if isinstance(row.get("subjects"), list):
                row["subjects"] = "|".join(str(s) for s in row["subjects"])
            w.writerow(row)


def _slugify(s: str, *, max_len: int = 100) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "item"


def _download_stream(url: str, dest: str, *, timeout_s: int, retries: int, backoff_s: float) -> None:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-repositori/1.0)"}
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
    items: List[Dict[str, Any]],
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
    n = len(items)
    for i, item in enumerate(items, start=1):
        item_id = str(item.get("id") or f"item_{i}")
        pdf_url = item.get("pdf_url")
        title = str(item.get("title") or item_id)
        author = str(item.get("author") or "")
        year = str(item.get("year") or "")
        date = str(item.get("date") or year)
        abstract = str(item.get("abstract") or "")
        publisher = str(item.get("publisher") or PUBLISHER)
        isbn = str(item.get("isbn") or "")
        item_type = str(item.get("type") or "")
        subjects_list = item.get("subjects", [])
        if isinstance(subjects_list, list):
            subjects_str = ", ".join(str(s) for s in subjects_list if s)
        else:
            subjects_str = str(subjects_list)

        if not pdf_url:
            manifest.append({
                "id": item_id,
                "title": title,
                "author": author,
                "year": year,
                "date": date,
                "publisher": publisher,
                "isbn": isbn,
                "type": item_type,
                "subjects": subjects_str,
                "abstract": abstract,
                "source_url": None,
                "file_path": None,
                "status": "failed",
                "error": "no pdf_url",
            })
            continue

        slug = _slugify(title) if title and title != item_id else item_id
        dest = os.path.join(files_dir, f"{slug}.pdf")

        def _embed(dest_path: str) -> None:
            if not embed_metadata:
                return
            try:
                embed_pdf_metadata(
                    dest_path,
                    title=title,
                    author=author,
                    subject=subjects_str or PUBLISHER,
                    keywords=subjects_str,
                    publisher=publisher or PUBLISHER,
                    publication_date=date or year,
                    write_xmp=True,
                    backup=embed_backup,
                )
                print(f"[pdf-meta] {item_id}")
            except Exception as ex:
                print(f"[pdf-meta] failed {item_id}: {ex}", file=sys.stderr)

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            manifest.append({
                "id": item_id,
                "title": title,
                "author": author,
                "year": year,
                "date": date,
                "publisher": publisher,
                "isbn": isbn,
                "type": item_type,
                "subjects": subjects_str,
                "abstract": abstract,
                "source_url": pdf_url,
                "file_path": dest,
                "status": "skipped",
            })
            print(f"[pdf] {i}/{n} {item_id} -> {dest} (skipped)")
            _embed(dest)
            continue

        try:
            _download_stream(pdf_url, dest, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
            manifest.append({
                "id": item_id,
                "title": title,
                "author": author,
                "year": year,
                "date": date,
                "publisher": publisher,
                "isbn": isbn,
                "type": item_type,
                "subjects": subjects_str,
                "abstract": abstract,
                "source_url": pdf_url,
                "file_path": dest,
                "status": "downloaded",
            })
            print(f"[pdf] {i}/{n} {item_id} -> {dest}")
            _embed(dest)
        except Exception as e:
            manifest.append({
                "id": item_id,
                "title": title,
                "author": author,
                "year": year,
                "date": date,
                "publisher": publisher,
                "isbn": isbn,
                "type": item_type,
                "subjects": subjects_str,
                "abstract": abstract,
                "source_url": pdf_url,
                "file_path": None,
                "status": "failed",
                "error": str(e),
            })
            print(f"[pdf] failed {i}/{n} {item_id}: {e}", file=sys.stderr)

        if sleep_s > 0:
            time.sleep(sleep_s)

    return manifest


def write_manifest(*, files_dir: str, manifest: List[Dict[str, Any]]) -> None:
    os.makedirs(files_dir, exist_ok=True)
    jpath = os.path.join(files_dir, "items_manifest.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    cpath = os.path.join(files_dir, "items_manifest.csv")
    fields = [
        "id", "title", "author", "year", "date", "publisher", "isbn", "type",
        "subjects", "abstract", "source_url", "file_path", "status", "error",
    ]
    with open(cpath, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in manifest:
            w.writerow(row)


def run_subject(
    subject_path: str,
    subject_name: str,
    base_out_dir: str,
    base_files_dir: str,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
    embed_metadata: bool,
    embed_backup: bool,
    parent_path: Optional[str] = None,
    parent_name: Optional[str] = None,
) -> None:
    folder_slug = _slugify(subject_name)
    if parent_name:
        parent_slug = _slugify(parent_name)
        out_dir = os.path.join(base_out_dir, parent_slug, folder_slug)
        files_dir = os.path.join(base_files_dir, parent_slug, folder_slug)
    else:
        out_dir = os.path.join(base_out_dir, folder_slug)
        files_dir = os.path.join(base_files_dir, folder_slug)

    meta, items, manifest = scrape_subject_page(
        subject_path=subject_path,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
        sleep_s=sleep_s,
        files_dir=files_dir,
        embed_metadata=embed_metadata,
        embed_backup=embed_backup,
    )
    print(f"[{folder_slug}] Scraped {len(items)} items, {sum(1 for m in manifest if m['status']=='downloaded')} PDFs downloaded.")
    write_outputs(out_dir=out_dir, meta=meta, items=items)
    print(f"[{folder_slug}] Wrote: {os.path.join(out_dir, 'items.json')} and items.csv")
    write_manifest(files_dir=files_dir, manifest=manifest)
    print(f"[{folder_slug}] Wrote: {os.path.join(files_dir, 'items_manifest.json')}")


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape repositori.kemendikdasmen.go.id")
    p.add_argument("--subject", default=None, help="Subject path (e.g., view/subjects/PED007=2E9.html). Omit to auto-discover all subjects.")
    p.add_argument("--out-dir", default="output/kemendikdasmen/subjects", help="Base output directory")
    p.add_argument("--timeout-s", type=int, default=60)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--backoff-s", type=float, default=0.5)
    p.add_argument("--sleep-s", type=float, default=0.15)

    p.add_argument("--files-dir", default="output/kemendikdasmen/files", help="Base files directory")
    p.add_argument(
        "--no-embed-pdf-metadata",
        action="store_true",
        help="Do not embed metadata into PDFs",
    )
    p.add_argument("--embed-backup", action="store_true", help="Create .bak before embedding")

    args = p.parse_args()
    embed_metadata = not args.no_embed_pdf_metadata

    if args.subject:
        if "/" in args.subject or args.subject.endswith(".html") or "=" in args.subject:
            subject_path = args.subject if args.subject.startswith("/") else "/" + args.subject
            found = _find_subject_by_path(subject_path, timeout_s=args.timeout_s, retries=args.retries, backoff_s=args.backoff_s)
            if found:
                subject_name = found["name"]
                parent_path = found.get("parent_path")
                parent_name = None
                if parent_path:
                    parent_found = _find_subject_by_path(parent_path, timeout_s=args.timeout_s, retries=args.retries, backoff_s=args.backoff_s)
                    if parent_found:
                        parent_name = parent_found["name"]
            else:
                subject_name = subject_path.split("/")[-1].replace(".html", "")
                parent_path = None
                parent_name = None
            run_subject(
                subject_path=subject_path,
                subject_name=subject_name,
                base_out_dir=args.out_dir,
                base_files_dir=args.files_dir,
                timeout_s=args.timeout_s,
                retries=args.retries,
                backoff_s=args.backoff_s,
                sleep_s=args.sleep_s,
                embed_metadata=embed_metadata,
                embed_backup=args.embed_backup,
                parent_path=parent_path,
                parent_name=parent_name,
            )
        else:
            found = _find_subject_by_name(args.subject, timeout_s=args.timeout_s, retries=args.retries, backoff_s=args.backoff_s)
            if not found:
                sys.exit(f"Subject not found: {args.subject}")
            parent_name = None
            if found.get("parent_path"):
                parent_found = _find_subject_by_path(found["parent_path"], timeout_s=args.timeout_s, retries=args.retries, backoff_s=args.backoff_s)
                if parent_found:
                    parent_name = parent_found["name"]
            run_subject(
                subject_path=found["path"],
                subject_name=found["name"],
                base_out_dir=args.out_dir,
                base_files_dir=args.files_dir,
                timeout_s=args.timeout_s,
                retries=args.retries,
                backoff_s=args.backoff_s,
                sleep_s=args.sleep_s,
                embed_metadata=embed_metadata,
                embed_backup=args.embed_backup,
                parent_path=found.get("parent_path"),
                parent_name=parent_name,
            )
        return

    subjects_html = _http_get(
        urllib.parse.urljoin(BASE_URL, "/view/subjects/"),
        timeout_s=args.timeout_s,
        retries=args.retries,
        backoff_s=args.backoff_s,
    )
    subjects = _discover_subjects(subjects_html)
    print(f"Discovered {len(subjects)} subjects")

    for s in subjects:
        parent_name = None
        if s.get("parent_path"):
            for ps in subjects:
                if ps["path"] == s["parent_path"]:
                    parent_name = ps["name"]
                    break
        run_subject(
            subject_path=s["path"],
            subject_name=s["name"],
            base_out_dir=args.out_dir,
            base_files_dir=args.files_dir,
            timeout_s=args.timeout_s,
            retries=args.retries,
            backoff_s=args.backoff_s,
            sleep_s=args.sleep_s,
            embed_metadata=embed_metadata,
            embed_backup=args.embed_backup,
            parent_path=s.get("parent_path"),
            parent_name=parent_name,
        )
        if args.sleep_s > 0:
            time.sleep(args.sleep_s)


if __name__ == "__main__":
    main()