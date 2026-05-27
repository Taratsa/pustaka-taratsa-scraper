"""
Embed title/author/subject/keywords into a PDF (Info dict + optional XMP).
Used by scrape_books.py and scrape_kabe.py (Calibre / Calibre-Web friendly).
"""
import os
import sys
from typing import Any, Optional, Tuple


def load_pypdf() -> Tuple[Any, Any]:
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
    except ImportError:
        pydeps = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pydeps")
        if os.path.isdir(pydeps):
            if pydeps not in sys.path:
                sys.path.insert(0, pydeps)
            from pypdf import PdfReader, PdfWriter  # type: ignore
        else:
            raise
    return PdfReader, PdfWriter


def build_xmp_packet(
    *,
    title: str,
    author: str,
    subject: str,
    keywords: str,
    publisher: str = "",
    creation_date: str = "",
) -> bytes:
    import xml.sax.saxutils as sx

    def esc(s: str) -> str:
        return sx.escape(s or "")

    publisher_block = ""
    if publisher.strip():
        publisher_block = f"""
      <dc:publisher>
        <rdf:Bag>
          <rdf:li>{esc(publisher)}</rdf:li>
        </rdf:Bag>
      </dc:publisher>"""

    date_block = ""
    if creation_date.strip():
        date_block = f"""
      <DC:date>
        <rdf:Bag>
          <rdf:li>{esc(creation_date)}</rdf:li>
        </rdf:Bag>
      </DC:date>"""

    xmp = f"""<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
           xmlns:dc='http://purl.org/dc/elements/1.1/'
           xmlns:DC='http://purl.org/dc/elements/1.1/'>
    <rdf:Description rdf:about=''>
      <dc:title>
        <rdf:Alt>
          <rdf:li xml:lang='x-default'>{esc(title)}</rdf:li>
        </rdf:Alt>
      </dc:title>
      <dc:creator>
        <rdf:Seq>
          <rdf:li>{esc(author)}</rdf:li>
        </rdf:Seq>
      </dc:creator>
      <dc:description>
        <rdf:Alt>
          <rdf:li xml:lang='x-default'>{esc(subject)}</rdf:li>
        </rdf:Alt>
      </dc:description>
      <dc:subject>
        <rdf:Bag>
          <rdf:li>{esc(keywords)}</rdf:li>
        </rdf:Bag>
      </dc:subject>{publisher_block}{date_block}
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""
    return xmp.encode("utf-8")


def embed_pdf_metadata(
    pdf_path: str,
    *,
    title: str = "",
    author: str = "",
    subject: str = "",
    keywords: str = "",
    publisher: str = "",
    publication_date: str = "",
    creation_date: str = "",
    write_xmp: bool = True,
    backup: bool = False,
) -> None:
    """Rewrite pdf_path in place with embedded metadata."""
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(pdf_path)
    if not pdf_path.lower().endswith(".pdf"):
        raise ValueError("not a PDF path")

    PdfReader, PdfWriter = load_pypdf()
    try:
        from pypdf.generic import DecodedStreamObject, NameObject  # type: ignore
    except Exception:
        DecodedStreamObject = None  # type: ignore
        NameObject = None  # type: ignore

    metadata = {}
    if title:
        metadata["/Title"] = title
    if author:
        metadata["/Author"] = author
    if subject:
        metadata["/Subject"] = subject
    if keywords:
        metadata["/Keywords"] = keywords
    # Standard PDF /Publisher field + XMP dc:publisher (both written for max compatibility)
    if publisher.strip():
        metadata["/Publisher"] = publisher.strip()
    # /Date holds the publication/creation date of the work (YYYYMMDD or YYYY only)
    # Written to both /Date (PDF spec) and dc:date (XMP/Dublin Core)
    if publication_date.strip():
        import re as _re
        pd = publication_date.strip()
        m = _re.match(r"(\d{4})-(\d{2})-(\d{2})", pd)
        if m:
            metadata["/Date"] = f"D:{m.group(1)}{m.group(2)}{m.group(3)}"
        elif _re.match(r"^\d{4}$", pd):
            metadata["/Date"] = f"D:{pd}"
    # Store file creation timestamp as /CreationDate (D:YYYYMMDDHHmmSS format)
    if creation_date.strip():
        import re as _re2
        cd = creation_date.strip()
        m2 = _re2.match(r"(\d{4})-(\d{2})-(\d{2})", cd)
        if m2:
            metadata["/CreationDate"] = f"D:{m2.group(1)}{m2.group(2)}{m2.group(3)}"
        elif _re2.match(r"^\d{4}$", cd.strip()):
            metadata["/CreationDate"] = f"D:{cd.strip()}"
    if not metadata:
        return

    if backup:
        bak_path = pdf_path + ".bak"
        if not os.path.exists(bak_path):
            with open(pdf_path, "rb") as src, open(bak_path, "wb") as dst:
                dst.write(src.read())

    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    writer.add_metadata(metadata)

    if write_xmp and DecodedStreamObject is not None and NameObject is not None:
        xmp_bytes = build_xmp_packet(
            title=title,
            author=author,
            subject=subject,
            keywords=keywords,
            publisher=publisher,
            creation_date=publication_date or creation_date,
        )
        try:
            xmp_stream = DecodedStreamObject()
            xmp_stream.set_data(xmp_bytes)
            xmp_stream.update(
                {
                    NameObject("/Type"): NameObject("/Metadata"),
                    NameObject("/Subtype"): NameObject("/XML"),
                }
            )
            writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp_stream)  # type: ignore[attr-defined]
        except Exception:
            pass

    tmp_path = pdf_path + ".meta.tmp"
    with open(tmp_path, "wb") as f:
        writer.write(f)
    os.replace(tmp_path, pdf_path)
