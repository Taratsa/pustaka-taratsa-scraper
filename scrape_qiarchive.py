#!/usr/bin/env python3
"""
Scrape Queer Indonesia Archive (qiarchive.org/id/berkas/)
Uses XML sitemaps for complete item list.
"""

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
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple


BASE_URL = "https://qiarchive.org"
SITEMAP_URLS = [
    "https://qiarchive.org/portfolio-item-sitemap1.xml",
    "https://qiarchive.org/portfolio-item-sitemap2.xml",
]


def _http_get(url: str, *, timeout_s: int, retries: int, backoff_s: float) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; scrape-qiarchive/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
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
    return s or "item"


def fetch_sitemap_urls(sitemap_url: str, *, timeout_s: int, retries: int, backoff_s: float) -> List[str]:
    """Fetch a sitemap XML and extract all /id/berkas/ URLs."""
    xml_content = _http_get(sitemap_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
    urls = []
    try:
        root = ET.fromstring(xml_content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for url_elem in root.findall("sm:url/sm:loc", ns):
            loc = url_elem.text or ""
            if "/id/berkas/" in loc:
                urls.append(loc)
    except ET.ParseError as e:
        print(f"XML parse error for {sitemap_url}: {e}", file=sys.stderr)
    return urls


def _parse_detail_page(html: str, url: str) -> Dict[str, Any]:
    """Extract all metadata from detail page."""
    item: Dict[str, Any] = {"source_url": url}

    # Title: <h2 class="edgtf-page-title entry-title"><span>Title</span></h2>
    title_pattern = re.compile(
        r'<h2[^>]+class="[^"]*edgtf-page-title[^"]*"[^>]*>.*?'
        r'<span[^>]*>([^<]+)</span>',
        re.DOTALL,
    )
    title_m = title_pattern.search(html)
    if title_m:
        item["title"] = title_m.group(1).strip()

    # Metadata fields: <h6 class="edgtf-ps-info-title">Label:</h6><p class="edgtf-ps-custom-item">Value</p>
    pairs_pattern = re.compile(
        r'<h6 class="edgtf-ps-info-title">\s*([^:]+):\s*</h6>\s*<p[^>]+class="edgtf-ps-custom-item"[^>]*>\s*([^<]+?)\s*</p>',
        re.DOTALL,
    )
    for pm in pairs_pattern.finditer(html):
        label = pm.group(1).strip().lower().replace(" ", "_")
        value = pm.group(2).strip()
        item[label] = value

    # Tags: <div class="... edgtf-ps-tags ..."> <a class="edgtf-ps-info-tag">Tag</a>
    tags_pattern = re.compile(
        r'<div[^>]*class="[^"]*edgtf-ps-tags[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL,
    )
    tags_m = tags_pattern.search(html)
    if tags_m:
        tags = re.findall(r'<a[^>]+class="edgtf-ps-info-tag"[^>]*>([^<]+)</a>', tags_m.group(1))
        item["tags"] = [t.strip() for t in tags if t.strip()]

    # Kategori: <div class="... edgtf-ps-categories ..."> <a class="edgtf-ps-info-category">Cat</a>
    kat_pattern = re.compile(
        r'<div[^>]*class="[^"]*edgtf-ps-categories[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL,
    )
    kat_m = kat_pattern.search(html)
    if kat_m:
        kats = re.findall(r'<a[^>]+class="edgtf-ps-info-category"[^>]*>([^<]+)</a>', kat_m.group(1))
        item["kategori"] = [k.strip() for k in kats if k.strip()]

    # Table of contents (Daftar Isi)
    toc_pattern = re.compile(r'<strong>Daftar Isi:</strong>(.*?)</p>', re.DOTALL)
    toc_m = toc_pattern.search(html)
    if toc_m:
        toc_text = toc_m.group(1)
        lines = []
        for line in re.split(r'<br\s*/?>', toc_text):
            line = re.sub(r'<[^>]+>', '', line).strip()
            if line:
                lines.append(line)
        item["daftar_isi"] = lines

    # Cover image - find first image in portfolio media section
    img_pattern = re.compile(
        r'<div[^>]+class="[^"]*edgtf-ps-image[^"]*"[^>]*>.*?<img[^>]+src="([^"]+)"',
        re.DOTALL,
    )
    img_m = img_pattern.search(html)
    if img_m:
        item["cover_image_url"] = img_m.group(1)

    # PDF source from DFLIP shortcode: window.option_df_XXXX = {"source":"url",...}
    dflip_pattern = re.compile(
        r'window\.option_df_\d+\s*=\s*(\{[^}]*"source"\s*:\s*"([^"]+)"[^}]*\})',
        re.DOTALL,
    )
    dflip_m = dflip_pattern.search(html)
    if dflip_m:
        try:
            opts = json.loads(dflip_m.group(1))
            item["pdf_url"] = opts.get("source", "")
        except json.JSONDecodeError:
            src_m = re.search(r'"source"\s*:\s*"([^"]+)"', dflip_m.group(1))
            if src_m:
                item["pdf_url"] = src_m.group(1)

    # Prev/next navigation
    prev_pattern = re.compile(r'\[Prev\]\s*</a>.*?href="(https://qiarchive\.org/id/berkas/([^"]+))"')
    prev_m = prev_pattern.search(html)
    if not prev_m:
        prev_pattern = re.compile(r'href="(https://qiarchive\.org/id/berkas/([^"]+))"[^>]*>.*?\[Prev\]', re.DOTALL)
        prev_m = prev_pattern.search(html)
    if prev_m:
        item["prev_url"] = prev_m.group(1)
        item["prev_slug"] = prev_m.group(2).strip("/")

    next_pattern = re.compile(r'\[Next\]\s*</a>.*?href="(https://qiarchive\.org/id/berkas/([^"]+))"')
    next_m = next_pattern.search(html)
    if not next_m:
        next_pattern = re.compile(r'href="(https://qiarchive\.org/id/berkas/([^"]+))"[^>]*>.*?\[Next\]', re.DOTALL)
        next_m = next_pattern.search(html)
    if next_m:
        item["next_url"] = next_m.group(1)
        item["next_slug"] = next_m.group(2).strip("/")

    # Extract slug from URL
    slug_m = re.search(r'/berkas/([^/]+)/', url)
    if slug_m:
        item["slug"] = slug_m.group(1)

    return item


def scrape_all_items(
    *,
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch all item URLs from sitemaps, then scrape each detail page."""
    all_urls: List[str] = []
    for sitemap_url in SITEMAP_URLS:
        urls = fetch_sitemap_urls(sitemap_url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
        all_urls.extend(urls)
        print(f"Fetched {len(urls)} URLs from {sitemap_url}")

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for url in all_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    print(f"Total unique /id/berkas/ URLs: {len(unique_urls)}")

    meta = {
        "base_url": BASE_URL,
        "sitemap_urls": SITEMAP_URLS,
        "total_urls_found": len(unique_urls),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    all_items: List[Dict[str, Any]] = []
    for i, url in enumerate(unique_urls, start=1):
        try:
            html = _http_get(url, timeout_s=timeout_s, retries=retries, backoff_s=backoff_s)
            item = _parse_detail_page(html, url)
            # Build a display title from available sources
            for title_key in ("judul", "title", "judul_koleksi"):
                if item.get(title_key):
                    item["display_title"] = item[title_key]
                    break
            else:
                item["display_title"] = "Unknown"

            all_items.append(item)
            display_title = item.get("judul") or item.get("title") or item.get("judul_koleksi") or "N/A"
            print(f"[{i}/{len(unique_urls)}] {item.get('slug', '?')}: {display_title}")
        except Exception as e:
            slug_m = re.search(r'/berkas/([^/]+)/', url)
            slug = slug_m.group(1) if slug_m else "?"
            print(f"[{i}/{len(unique_urls)}] FAILED {slug}: {e}", file=sys.stderr)
            all_items.append({"slug": slug, "source_url": url, "error": str(e)})

        if sleep_s > 0 and i < len(unique_urls):
            time.sleep(sleep_s)

    meta["total_items"] = len(all_items)
    meta["successful"] = sum(1 for item in all_items if "error" not in item)
    return meta, all_items


def write_outputs(*, out_dir: str, meta: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "qiarchive.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "items": items}, f, ensure_ascii=False, indent=2)

    # CSV with common fields
    preferred_order = [
        "slug",
        "title",
        "judul",
        "penerbit",
        "tanggal",
        "lokasi_penerbit",
        "sumber",
        "tags",
        "kategori",
        "pdf_url",
        "cover_image_url",
        "source_url",
        "prev_slug",
        "next_slug",
    ]

    fieldnames_set = set()
    for item in items:
        fieldnames_set.update(item.keys())

    fieldnames = [k for k in preferred_order if k in fieldnames_set] + sorted(fieldnames_set - set(preferred_order))

    csv_path = os.path.join(out_dir, "qiarchive.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            row = dict(item)
            # Convert list fields to pipe-separated for CSV
            for key in ("tags", "kategori", "daftar_isi"):
                val = row.get(key)
                if isinstance(val, list):
                    row[key] = "|".join([str(v) for v in val])
            writer.writerow(row)


def download_pdfs(
    *,
    out_dir: str,
    items: List[Dict[str, Any]],
    timeout_s: int,
    retries: int,
    backoff_s: float,
    sleep_s: float,
) -> List[Dict[str, Any]]:
    """Download PDFs for items that have pdf_url."""
    os.makedirs(out_dir, exist_ok=True)
    manifest: List[Dict[str, Any]] = []

    items_with_pdf = [item for item in items if item.get("pdf_url") and "error" not in item]
    total = len(items_with_pdf)
    for i, item in enumerate(items_with_pdf, start=1):
        pdf_url = item.get("pdf_url")
        slug = item.get("slug", "unknown")
        title = item.get("title", "")
        safe = _slugify(title) if title else slug

        prefix = f"{slug}-{safe}" if slug else safe
        ext_path = os.path.join(out_dir, prefix)

        # Check if already downloaded
        for ext in ("pdf",):
            candidate = f"{ext_path}.{ext}"
            if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                manifest.append({
                    "slug": slug,
                    "title": title,
                    "pdf_url": pdf_url,
                    "file_path": candidate,
                    "status": "skipped",
                })
                print(f"[pdf] {i}/{total} {slug} -> {candidate} (skipped)")
                break
        else:
            # Download
            try:
                headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-qiarchive/1.0)"}
                req = urllib.request.Request(pdf_url, headers=headers, method="GET")
                tmp_path = os.path.join(out_dir, prefix + ".pdf.part")
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    with open(tmp_path, "wb") as f:
                        while True:
                            chunk = resp.read(1024 * 128)
                            if not chunk:
                                break
                            f.write(chunk)
                final_path = os.path.join(out_dir, prefix + ".pdf")
                if os.path.exists(final_path):
                    os.remove(final_path)
                os.replace(tmp_path, final_path)
                manifest.append({
                    "slug": slug,
                    "title": title,
                    "pdf_url": pdf_url,
                    "file_path": final_path,
                    "status": "downloaded",
                })
                print(f"[pdf] {i}/{total} {slug} -> {final_path}")
            except Exception as e:
                manifest.append({
                    "slug": slug,
                    "title": title,
                    "pdf_url": pdf_url,
                    "file_path": None,
                    "status": "failed",
                    "error": str(e),
                })
                print(f"[pdf] failed {i}/{total} {slug}: {e}", file=sys.stderr)

        if sleep_s > 0:
            time.sleep(sleep_s)

    return manifest


def write_pdf_manifest(*, out_dir: str, manifest: List[Dict[str, Any]]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "pdf_manifest.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(out_dir, "pdf_manifest.csv")
    fieldnames = ["slug", "title", "pdf_url", "file_path", "status", "error"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape qiarchive.org/id/berkas/ via XML sitemaps")
    parser.add_argument("--out-dir", default="output/qiarchive", help="Output directory (default: output/qiarchive)")
    parser.add_argument("--timeout-s", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff-s", type=float, default=0.5)
    parser.add_argument("--sleep-s", type=float, default=0.3, help="Sleep between item requests")
    parser.add_argument("--download-pdfs", action="store_true", help="Download PDFs")
    parser.add_argument("--pdfs-dir", default="output/qiarchive/pdfs", help="PDF download directory")

    args = parser.parse_args()

    print("Fetching item URLs from sitemaps...")
    meta, items = scrape_all_items(
        timeout_s=args.timeout_s,
        retries=args.retries,
        backoff_s=args.backoff_s,
        sleep_s=args.sleep_s,
    )

    print(f"Scraped {len(items)} items ({meta.get('successful', '?')} successful).")
    write_outputs(out_dir=args.out_dir, meta=meta, items=items)
    print(f"Wrote: {os.path.join(args.out_dir, 'qiarchive.json')} and {os.path.join(args.out_dir, 'qiarchive.csv')}")

    if args.download_pdfs:
        print(f"Downloading PDFs into: {args.pdfs_dir}")
        manifest = download_pdfs(
            out_dir=args.pdfs_dir,
            items=items,
            timeout_s=args.timeout_s,
            retries=args.retries,
            backoff_s=args.backoff_s,
            sleep_s=args.sleep_s,
        )
        write_pdf_manifest(out_dir=args.pdfs_dir, manifest=manifest)
        print("PDF download complete.")


if __name__ == "__main__":
    main()