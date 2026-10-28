"""
Download one Rekhta ebook to a PDF.

Change BOOK_URL to the address-bar link of the book, then from the project folder run:
    python scripts/run_book.py

Stitches every page, writes the PDF only if the book is complete, then deletes
the individual page PNGs. If stitching or the PDF step fails, the PNGs stay.
The books folder is created on this run if it is not there yet.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from scripts.stitch_ebook import (
    BROWSER_HEADERS,
    DEFAULT_RATE_PER_SEC,
    DEFAULT_WORKERS,
    book_id_from_page,
    save_pages,
    slug_from_ebook_url,
)
from scripts.to_pdf import catalog_length, page_png, write_pdf

# Paste the ebook URL from the browser address bar.
BOOK_URL = "https://www.rekhta.org/ebooks/gharelu-tashaddud-aur-islam-dr-mohammad-razi-ul-islam-nadwi-ebooks"

OUTPUT_ROOT = ROOT / "books"


def delete_page_pngs(output_dir: Path, total: int) -> None:
    for page_num in range(1, total + 1):
        path = page_png(output_dir, page_num)
        if path.is_file():
            path.unlink()


def main() -> None:
    slug = slug_from_ebook_url(BOOK_URL)
    output_dir = OUTPUT_ROOT / slug
    output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    bk_id = book_id_from_page(session, BOOK_URL)
    print(f"book {slug}")
    print(f"bkId {bk_id}")

    save_pages(
        bk_id=bk_id,
        slug=slug,
        output_dir=output_dir,
        start_page=1,
        end_page=None,
        rate_per_sec=DEFAULT_RATE_PER_SEC,
        workers=DEFAULT_WORKERS,
    )

    pdf_path = output_dir / f"{slug}.pdf"
    total = catalog_length(slug)
    write_pdf(output_dir, pdf_path, total)
    delete_page_pngs(output_dir, total)
    print(f"removed {total} page PNGs; kept {pdf_path}")


if __name__ == "__main__":
    main()
