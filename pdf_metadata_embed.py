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

    xmp = f"""<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
           xmlns:dc='http://purl.org/dc/elements/1.1/'>
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
      </dc:subject>{publisher_block}
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
    # Non-standard but often shown as “Company” in some viewers; XMP dc:publisher is primary.
    if publisher.strip():
        metadata["/Company"] = publisher.strip()
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
