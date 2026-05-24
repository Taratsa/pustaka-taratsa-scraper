#!/usr/bin/env python3
"""
Clear entrypoint for Langka Logos API scraping.

This wrapper preserves behavior by delegating to scrape_books.py.
"""

from scrape_books import main


if __name__ == "__main__":
    main()
