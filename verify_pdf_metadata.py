#!/usr/bin/env python3
import argparse
import csv
import json
import os
from typing import Dict, List, Tuple

from pdf_metadata_embed import load_pypdf


def _collect_pdf_paths(target_dir: str) -> List[str]:
    out: List[str] = []
    for name in sorted(os.listdir(target_dir)):
        p = os.path.join(target_dir, name)
        if os.path.isfile(p) and name.lower().endswith(".pdf"):
            out.append(p)
    return out


def _check_one(path: str) -> Dict[str, object]:
    PdfReader, _ = load_pypdf()
    row: Dict[str, object] = {
        "file_path": os.path.abspath(path),
        "has_info_title": False,
        "has_info_author": False,
        "has_info_subject": False,
        "has_info_keywords": False,
        "has_info_publisher": False,
        "has_xmp": False,
        "status": "ok",
        "error": "",
    }
    try:
        r = PdfReader(path)
        md = r.metadata or {}
        title = str(md.get("/Title") or "").strip()
        author = str(md.get("/Author") or "").strip()
        subject = str(md.get("/Subject") or "").strip()
        keywords = str(md.get("/Keywords") or "").strip()
        publisher = str(md.get("/Company") or "").strip()
        has_xmp = bool(getattr(r, "xmp_metadata", None))

        row["has_info_title"] = bool(title)
        row["has_info_author"] = bool(author)
        row["has_info_subject"] = bool(subject)
        row["has_info_keywords"] = bool(keywords)
        row["has_info_publisher"] = bool(publisher)
        row["has_xmp"] = has_xmp

        if not (row["has_info_title"] and row["has_info_author"] and row["has_info_publisher"] and row["has_xmp"]):
            row["status"] = "missing_required"
    except Exception as e:
        row["status"] = "error"
        row["error"] = str(e)
    return row


def _summary(rows: List[Dict[str, object]]) -> Tuple[int, int, int]:
    ok = sum(1 for r in rows if r["status"] == "ok")
    missing = sum(1 for r in rows if r["status"] == "missing_required")
    errs = sum(1 for r in rows if r["status"] == "error")
    return ok, missing, errs


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit embedded PDF metadata for Calibre-Web ingestion.")
    parser.add_argument("--dir", required=True, help="Directory containing PDF files")
    parser.add_argument("--out-json", default=None, help="Output JSON report path (default: <dir>/metadata_audit.json)")
    parser.add_argument("--out-csv", default=None, help="Output CSV report path (default: <dir>/metadata_audit.csv)")
    args = parser.parse_args()

    target_dir = args.dir
    if not os.path.isdir(target_dir):
        raise NotADirectoryError(target_dir)

    out_json = args.out_json or os.path.join(target_dir, "metadata_audit.json")
    out_csv = args.out_csv or os.path.join(target_dir, "metadata_audit.csv")

    pdfs = _collect_pdf_paths(target_dir)
    rows = [_check_one(p) for p in pdfs]
    ok, missing, errs = _summary(rows)

    payload = {
        "target_dir": os.path.abspath(target_dir),
        "total_pdfs": len(pdfs),
        "ok": ok,
        "missing_required": missing,
        "errors": errs,
        "rows": rows,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    fieldnames = [
        "file_path",
        "has_info_title",
        "has_info_author",
        "has_info_subject",
        "has_info_keywords",
        "has_info_publisher",
        "has_xmp",
        "status",
        "error",
    ]
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Audited {len(pdfs)} PDF(s)")
    print(f"ok={ok} missing_required={missing} errors={errs}")
    print(f"JSON: {out_json}")
    print(f"CSV:  {out_csv}")


if __name__ == "__main__":
    main()
