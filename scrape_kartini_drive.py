#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional

from pdf_metadata_embed import embed_pdf_metadata


DEFAULT_SOURCE = "output/api_kartini.json"
DEFAULT_OUT_DIR = "output/api_kartini_files"
DEFAULT_AUTHOR = "Majalah Api Kartini"
DEFAULT_SUBJECT = "Api Kartini"
DEFAULT_PUBLISHER = "Jajasan Melati"


def _load_items(source_path: str) -> List[Dict[str, Any]]:
    with open(source_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"Expected JSON object with 'items' list in {source_path}")
    return [dict(x) for x in items if isinstance(x, dict)]


def _issue_title(year: Any, no: Any) -> str:
    year_s = str(year or "").strip()
    no_s = str(no or "").strip()
    if year_s and no_s:
        return f"Api Kartini ({year_s}) No. {no_s}"
    if year_s:
        return f"Api Kartini ({year_s})"
    if no_s:
        return f"Api Kartini No. {no_s}"
    return "Api Kartini"


def _extract_confirm_token(html: str) -> str:
    m = re.search(r"confirm=([0-9A-Za-z_]+)", html)
    return m.group(1) if m else ""


def _infer_filename(content_disposition: str, fallback: str) -> str:
    if not content_disposition:
        return fallback
    m = re.search(r"filename\\*=UTF-8''([^;]+)", content_disposition)
    if m:
        return urllib.parse.unquote(m.group(1)).strip('"')
    m = re.search(r'filename="?([^";]+)"?', content_disposition)
    if m:
        return m.group(1)
    return fallback


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = f"{root}-{i}{ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def _existing_download_map(manifest_path: str) -> Dict[str, str]:
    if not os.path.isfile(manifest_path):
        return {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return {}
    if not isinstance(manifest, list):
        return {}
    out: Dict[str, str] = {}
    for row in manifest:
        if not isinstance(row, dict):
            continue
        drive_id = str(row.get("drive_id") or "").strip()
        status = str(row.get("status") or "")
        path = str(row.get("file_path") or "").strip()
        if not drive_id or not path:
            continue
        if status not in ("downloaded", "downloaded+metadata", "skipped"):
            continue
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            out[drive_id] = path
    return out


def _download_file_from_drive(
    file_id: str,
    *,
    timeout_s: int,
) -> Dict[str, Any]:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {"User-Agent": "Mozilla/5.0"}

    base = f"https://drive.google.com/uc?export=download&id={urllib.parse.quote(file_id)}"
    req = urllib.request.Request(base, headers=headers, method="GET")
    with opener.open(req, timeout=timeout_s) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        content_disposition = resp.headers.get("Content-Disposition") or ""
        body = resp.read()

    # Google Drive may return an interstitial HTML page with a confirm token.
    if "text/html" in content_type and b"confirm=" in body:
        html = body.decode("utf-8", errors="replace")
        token = _extract_confirm_token(html)
        if token:
            q = urllib.parse.urlencode({"export": "download", "confirm": token, "id": file_id})
            req2 = urllib.request.Request(f"https://drive.google.com/uc?{q}", headers=headers, method="GET")
            with opener.open(req2, timeout=timeout_s) as resp2:
                content_type = (resp2.headers.get("Content-Type") or "").lower()
                content_disposition = resp2.headers.get("Content-Disposition") or ""
                body = resp2.read()

    return {
        "content_type": content_type,
        "content_disposition": content_disposition,
        "body": body,
    }


def download_all(
    *,
    items: List[Dict[str, Any]],
    out_dir: str,
    timeout_s: int,
    author: str,
    subject: str,
    publisher: str,
    embed_metadata: bool,
    embed_backup: bool,
    embed_xmp: bool,
    existing_downloads: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    os.makedirs(out_dir, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    existing_downloads = existing_downloads or {}

    for i, item in enumerate(items, start=1):
        file_id = str(item.get("drive") or "").strip()
        index = item.get("index")
        year = item.get("year")
        no = item.get("no")
        title = _issue_title(year, no)
        tags = [subject, f"year:{year}" if year else "", f"issue:{no}" if no else ""]
        tags = [t for t in tags if t]

        row: Dict[str, Any] = {
            "index": index,
            "year": year,
            "no": no,
            "drive_id": file_id,
            "title": title,
            "author": author,
            "category": subject,
            "tags": tags,
            "edit_url": f"https://drive.google.com/file/d/{file_id}/edit",
            "source_url": str(item.get("page_url") or ""),
            "file_path": "",
            "status": "failed",
            "error": "",
        }

        if not file_id:
            row["error"] = "missing drive id"
            manifest.append(row)
            print(f"[{i}/{len(items)}] missing drive id", file=sys.stderr)
            continue

        prior_path = existing_downloads.get(file_id)
        if prior_path and os.path.isfile(prior_path) and os.path.getsize(prior_path) > 0:
            row["file_path"] = os.path.abspath(prior_path)
            row["status"] = "skipped"
            manifest.append(row)
            print(f"[{i}/{len(items)}] skipped {file_id} -> {os.path.basename(prior_path)}")
            continue

        try:
            dl = _download_file_from_drive(file_id, timeout_s=timeout_s)
            body = dl["body"]
            ct = dl["content_type"]
            cd = dl["content_disposition"]

            fallback = f"{int(index):03d}-{year}-no-{str(no).replace('/', '-')}.pdf" if isinstance(index, int) else f"{file_id}.pdf"
            filename = _infer_filename(cd, fallback)
            if "." not in filename:
                filename += ".pdf" if "pdf" in ct else ".bin"

            dest = _unique_path(os.path.join(out_dir, filename))
            with open(dest, "wb") as f:
                f.write(body)

            if os.path.getsize(dest) == 0 or ("text/html" in ct and body[:256].lower().startswith(b"<!doctype html")):
                row["error"] = f"got html/empty response (content-type={ct})"
                try:
                    os.remove(dest)
                except OSError:
                    pass
                manifest.append(row)
                print(f"[{i}/{len(items)}] failed {file_id}: {row['error']}", file=sys.stderr)
                continue

            row["file_path"] = os.path.abspath(dest)
            row["status"] = "downloaded"

            if embed_metadata and dest.lower().endswith(".pdf"):
                keywords = ", ".join(tags)
                embed_pdf_metadata(
                    dest,
                    title=title,
                    author=author,
                    subject=subject,
                    keywords=keywords,
                    publisher=publisher,
                    write_xmp=embed_xmp,
                    backup=embed_backup,
                )
                row["status"] = "downloaded+metadata"

            print(f"[{i}/{len(items)}] ok {file_id} -> {os.path.basename(dest)}")
        except Exception as e:
            row["error"] = str(e)
            print(f"[{i}/{len(items)}] failed {file_id}: {e}", file=sys.stderr)

        manifest.append(row)

    return manifest


def write_manifest(out_dir: str, manifest: List[Dict[str, Any]]) -> None:
    json_path = os.path.join(out_dir, "download_manifest.json")
    csv_path = os.path.join(out_dir, "download_manifest.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    fields = [
        "index",
        "year",
        "no",
        "drive_id",
        "title",
        "author",
        "category",
        "tags",
        "edit_url",
        "source_url",
        "file_path",
        "status",
        "error",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in manifest:
            out = dict(row)
            if isinstance(out.get("tags"), list):
                out["tags"] = "|".join(str(x) for x in out["tags"])
            w.writerow(out)


def embed_metadata_from_manifest(
    *,
    manifest_path: str,
    publisher: str,
    backup: bool,
    write_xmp: bool,
    limit: Optional[int],
) -> int:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest, list):
        raise ValueError("Manifest must be a JSON list")

    updated = 0
    for row in manifest:
        if limit is not None and updated >= limit:
            break
        if row.get("status") not in ("downloaded", "downloaded+metadata", "skipped"):
            continue
        path = row.get("file_path")
        if not isinstance(path, str) or not path.lower().endswith(".pdf"):
            continue
        if not os.path.exists(path):
            continue

        title = str(row.get("title") or "")
        author = str(row.get("author") or "")
        category = str(row.get("category") or "")
        tags = row.get("tags") or []
        keywords = ", ".join([str(t) for t in tags if t]) if isinstance(tags, list) else str(tags)

        try:
            embed_pdf_metadata(
                path,
                title=title,
                author=author,
                subject=category,
                keywords=keywords,
                publisher=publisher,
                write_xmp=write_xmp,
                backup=backup,
            )
            updated += 1
            print(f"[pdf-meta] updated: {path}")
        except Exception as e:
            print(f"[pdf-meta] failed {path}: {e}", file=sys.stderr)
    return updated


def embed_metadata_from_csv_manifest(
    *,
    csv_manifest_path: str,
    files_dir: str,
    author: str,
    subject: str,
    publisher: str,
    backup: bool,
    write_xmp: bool,
    limit: Optional[int],
) -> int:
    updated = 0
    with open(csv_manifest_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if limit is not None and updated >= limit:
            break
        if str(row.get("status") or "").lower() != "ok":
            continue
        saved_file = str(row.get("saved_file") or "").strip()
        if not saved_file:
            continue
        pdf_path = os.path.join(files_dir, saved_file)
        if not pdf_path.lower().endswith(".pdf"):
            continue
        if not os.path.exists(pdf_path):
            continue

        title = _issue_title(row.get("year"), row.get("no"))
        tags = [subject]
        if row.get("year"):
            tags.append(f"year:{row.get('year')}")
        if row.get("no"):
            tags.append(f"issue:{row.get('no')}")
        keywords = ", ".join(tags)

        try:
            embed_pdf_metadata(
                pdf_path,
                title=title,
                author=author,
                subject=subject,
                keywords=keywords,
                publisher=publisher,
                write_xmp=write_xmp,
                backup=backup,
            )
            updated += 1
            print(f"[pdf-meta] updated: {pdf_path}")
        except Exception as e:
            print(f"[pdf-meta] failed {pdf_path}: {e}", file=sys.stderr)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Download all Api Kartini Google Drive files and embed PDF metadata.")
    parser.add_argument("--source-json", default=DEFAULT_SOURCE, help=f"Path to api_kartini.json (default: {DEFAULT_SOURCE})")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"Download directory (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--timeout-s", type=int, default=30, help="HTTP timeout in seconds")

    parser.add_argument("--author", default=DEFAULT_AUTHOR, help=f"Author metadata for PDFs (default: {DEFAULT_AUTHOR})")
    parser.add_argument("--subject", default=DEFAULT_SUBJECT, help=f"Subject/category metadata (default: {DEFAULT_SUBJECT})")
    parser.add_argument("--publisher", default=DEFAULT_PUBLISHER, help=f"Publisher metadata for PDFs (default: {DEFAULT_PUBLISHER})")
    parser.add_argument("--no-embed-metadata", action="store_true", help="Skip embedding PDF metadata during download")
    parser.add_argument("--embed-backup", action="store_true", help="Create .bak files before metadata rewrite")
    parser.add_argument(
        "--no-xmp",
        action="store_true",
        help="Disable XMP metadata packet. By default XMP is written for Calibre-Web compatibility.",
    )

    parser.add_argument(
        "--embed-metadata-only",
        action="store_true",
        help="Do not download; only embed metadata using an existing JSON manifest",
    )
    parser.add_argument(
        "--download-files",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download missing files from Google Drive (default: enabled). Use --no-download-files to skip.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to download_manifest.json (default: <out-dir>/download_manifest.json)",
    )
    parser.add_argument("--embed-limit", type=int, default=None, help="Only update first N PDFs in metadata-only mode")
    args = parser.parse_args()

    if args.embed_metadata_only:
        json_manifest = args.manifest or os.path.join(args.out_dir, "download_manifest.json")
        csv_manifest = os.path.join(args.out_dir, "download_manifest.csv")
        if os.path.exists(json_manifest):
            updated = embed_metadata_from_manifest(
                manifest_path=json_manifest,
                publisher=args.publisher,
                backup=args.embed_backup,
                write_xmp=(not args.no_xmp),
                limit=args.embed_limit,
            )
        elif os.path.exists(csv_manifest):
            updated = embed_metadata_from_csv_manifest(
                csv_manifest_path=csv_manifest,
                files_dir=args.out_dir,
                author=args.author,
                subject=args.subject,
                publisher=args.publisher,
                backup=args.embed_backup,
                write_xmp=(not args.no_xmp),
                limit=args.embed_limit,
            )
        else:
            raise FileNotFoundError(f"Manifest not found: {json_manifest} or {csv_manifest}")
        print(f"PDF metadata updated for {updated} file(s).")
        return

    if not args.download_files:
        print("Download skipped (--no-download-files).")
        return

    items = _load_items(args.source_json)
    prior_manifest_path = os.path.join(args.out_dir, "download_manifest.json")
    existing = _existing_download_map(prior_manifest_path)
    manifest = download_all(
        items=items,
        out_dir=args.out_dir,
        timeout_s=args.timeout_s,
        author=args.author,
        subject=args.subject,
        publisher=args.publisher,
        embed_metadata=(not args.no_embed_metadata),
        embed_backup=args.embed_backup,
        embed_xmp=(not args.no_xmp),
        existing_downloads=existing,
    )
    write_manifest(args.out_dir, manifest)

    downloaded = sum(1 for r in manifest if str(r.get("status", "")).startswith("downloaded"))
    skipped = sum(1 for r in manifest if str(r.get("status", "")) == "skipped")
    failed = sum(1 for r in manifest if str(r.get("status", "")) == "failed")
    print(f"Download complete: total={len(manifest)} downloaded={downloaded} skipped={skipped} failed={failed}")
    print(f"Wrote manifest: {os.path.join(args.out_dir, 'download_manifest.json')}")
    print(f"Wrote manifest: {os.path.join(args.out_dir, 'download_manifest.csv')}")


if __name__ == "__main__":
    main()
