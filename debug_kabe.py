#!/usr/bin/env python3
"""Debug download issue with kabe PDFs"""
import os, sys, time, socket
os.environ["SSL_CERT_FILE"] = ""
socket.setdefaulttimeout(10)

import urllib.request, urllib.parse, ssl
ssl._create_default_https_context = ssl._create_unverified_context

BASE_URL = "https://kabe.drepram.com"
PDF_PATH = "/api/documents/file/Sobron%20Aidit%20-%20Abu%20(1962)-1.pdf"
pdf_url = urllib.parse.urljoin(BASE_URL, PDF_PATH)
print(f"Testing: {pdf_url}", flush=True)

dest = "/tmp/test_kabe2.pdf"
tmp = dest + ".part"
headers = {"User-Agent": "Mozilla/5.0 (compatible; scrape-kabe/1.0)"}

start = time.time()
req = urllib.request.Request(pdf_url, headers=headers, method="GET")

try:
    print("Opening connection...", flush=True)
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.status
        size = resp.headers.get("Content-Length", "unknown")
        print(f"Status: {status}, Size: {size}", flush=True)
        print(f"Connected in {time.time()-start:.1f}s", flush=True)

        bytes_read = 0
        with open(tmp, "wb") as out:
            while True:
                chunk = resp.read(1024 * 128)
                if not chunk:
                    break
                out.write(chunk)
                bytes_read += len(chunk)
                print(f"  Read {bytes_read} bytes...", flush=True)

        print(f"Total: {bytes_read} bytes in {time.time()-start:.1f}s", flush=True)
        if bytes_read > 0:
            os.replace(tmp, dest)
            print(f"Saved to {dest}, size={os.path.getsize(dest)}", flush=True)
except Exception as e:
    print(f"Error after {time.time()-start:.1f}s: {e}", flush=True)
    if os.path.exists(tmp):
        os.remove(tmp)