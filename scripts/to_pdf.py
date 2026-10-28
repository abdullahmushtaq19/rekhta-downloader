"""
Build a PDF from stitched page PNGs only when the book is complete.

Refuses to write anything if any page from 1 through the last catalog id is missing.
A stitch run that stops midway never reaches this step, and running this script
on a half-finished folder exits without creating a PDF.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import requests
from PIL import Image

from scripts.stitch_ebook import (
    BROWSER_HEADERS,
    DEFAULT_SLUG,
    DEFAULT_WORKERS,
    RateLimiter,
    fetch_page_catalog,
    slug_from_ebook_url,
)


def page_png(output_dir: Path, page_num: int) -> Path:
    return output_dir / f"page_{page_num:03d}.png"


def missing_pages(output_dir: Path, total: int) -> list[int]:
    missing: list[int] = []
    for page_num in range(1, total + 1):
        path = page_png(output_dir, page_num)
        if not path.is_file() or path.stat().st_size <= 0:
            missing.append(page_num)
    return missing


def write_pdf(output_dir: Path, pdf_path: Path, total: int) -> None:
    missing = missing_pages(output_dir, total)
    if missing:
        preview = ", ".join(str(n) for n in missing[:12])
        extra = "" if len(missing) <= 12 else f" … (+{len(missing) - 12} more)"
        raise SystemExit(
            f"Not writing a PDF. {len(missing)} of {total} pages are missing "
            f"({preview}{extra}). Stitch the rest first."
        )

    images = [Image.open(page_png(output_dir, n)).convert("RGB") for n in range(1, total + 1)]
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    first, rest = images[0], images[1:]
    first.save(pdf_path, save_all=True, append_images=rest)
    for image in images:
        image.close()
    print(f"PDF saved: {pdf_path} ({total} pages)")


def catalog_length(slug: str) -> int:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    _names, ids = fetch_page_catalog(session, RateLimiter(2.0, burst=2), slug, limit=None)
    if not ids:
        raise SystemExit("Catalog is empty; not writing a PDF.")
    return len(ids)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PDF from a finished stitch folder")
    parser.add_argument("--output-dir", type=Path, default=Path("books"))
    parser.add_argument("--pdf", type=Path, default=None, help="Output PDF path")
    parser.add_argument("--slug", default=DEFAULT_SLUG)
    parser.add_argument("--ebook-url", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    slug = slug_from_ebook_url(args.ebook_url) if args.ebook_url else args.slug
    total = catalog_length(slug)
    pdf_path = args.pdf or (args.output_dir / f"{slug}.pdf")
    print(f"book length is {total} pages")
    write_pdf(args.output_dir, pdf_path, total)
