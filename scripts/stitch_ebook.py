"""
Rekhta ebook pages: fetch GetEbookFromApi layout + tile sprite JPG, stitch to full PNG.

Viewer logic (renderEbookPage):
  drawImage(sprite, X1 * 66, Y1 * 66, 50, 50,  X2 * 50, Y2 * 50, 50, 50)
Sprite URL: https://ebooksapi.rekhta.org/images/{bkId}/{pages[n]}  (e.g. 001.jpg)
"""

from __future__ import annotations

import argparse
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
from PIL import Image

SOURCE_TILE = 50
SOURCE_STRIDE = 66  # 50 + 16 in Rekhta JS
REKHTA_ORIGIN = "https://www.rekhta.org"
TILE_CDN = "https://ebooksapi.rekhta.org/images"

DEFAULT_BK_ID = "413f1b4f-edcc-4776-b3c9-0a04d764a335"
DEFAULT_SLUG = "tableegh-e-deen-al-salasain-minal-arbayeen-maulana-ashraf-ali-thanvi-ebooks"


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,image/avif,image/webp,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.rekhta.org/",
}
RATE_HEADER_NAMES = (
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
)
DEFAULT_RATE_PER_SEC = 2.0
DEFAULT_WORKERS = 3
# The book page opens the connection. The page-list call right after that is the one
# Rekhta answers with Retry-After: 60. A short pause here is cheaper than that minute.
FIRST_API_PAUSE = 5.0


class RateLimiter:
    """Token bucket. wait() blocks until one request is allowed."""

    def __init__(self, rate_per_sec: float, burst: int = 1, min_rate: float = 0.2):
        self.rate = rate_per_sec
        self.capacity = float(burst)
        self.min_rate = min_rate
        self.tokens = float(burst)
        self.not_before = 0.0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                if now < self.not_before:
                    sleep_s = self.not_before - now
                else:
                    self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                    self.last = now
                    if self.tokens >= 1:
                        self.tokens -= 1
                        return
                    sleep_s = (1 - self.tokens) / self.rate
            time.sleep(sleep_s)

    def penalize(self, seconds: float, *, slow: bool = True) -> None:
        """Hold every waiter for the backoff. Halve the pace only when Rekhta did not say how long to wait."""
        with self.lock:
            if slow:
                self.rate = max(self.min_rate, self.rate / 2)
                print(f"  limiter slowed to {self.rate:.2f} req/s")
            self.tokens = 0
            self.not_before = max(self.not_before, time.monotonic() + seconds)
            self.last = time.monotonic()


_header_note_lock = threading.Lock()
_saw_rate_headers = False


def note_rate_headers(response: requests.Response) -> None:
    global _saw_rate_headers
    found = {
        key: response.headers[key]
        for key in response.headers
        if key.lower() in RATE_HEADER_NAMES
    }
    with _header_note_lock:
        if found:
            _saw_rate_headers = True
            print(f"  {response.status_code} rate headers: {found}")
        elif not _saw_rate_headers:
            print("  no Retry-After / X-RateLimit-* headers on responses so far")
            _saw_rate_headers = True


def _notify(on_status: Optional[Callable[[dict], None]], payload: dict) -> None:
    if on_status is None:
        return
    try:
        on_status(payload)
    except Exception as exc:
        print(f"  progress update failed: {exc}")


def fetch(
    session: requests.Session,
    url: str,
    limiter: RateLimiter,
    on_status: Optional[Callable[[dict], None]] = None,
) -> requests.Response:
    """Keep trying on 429 and dropped connections. Slow the shared limiter; do not abort the run."""
    attempt = 0
    held_until = 0.0
    while True:
        limiter.wait()
        try:
            response = session.get(url, timeout=60)
        except requests.RequestException as exc:
            attempt += 1
            delay = min(60.0, float(2 ** min(attempt, 6))) + random.random()
            held_until = time.time() + delay
            print(f"  connection error ({exc.__class__.__name__}), retry in {delay:.1f}s")
            _notify(
                on_status,
                {
                    "notice": "The connection dropped. That's all right — retrying, and this download keeps going.",
                    "mood": "",
                    "retry_until": held_until,
                },
            )
            time.sleep(delay)
            continue
        note_rate_headers(response)
        if response.status_code == 429 or response.status_code >= 500:
            attempt += 1
            raw_delay = response.headers.get("Retry-After", "")
            try:
                delay = float(raw_delay) if raw_delay else float(2 ** min(attempt, 6))
            except ValueError:
                delay = float(2 ** min(attempt, 6))
            delay = min(60.0, delay) + random.random()
            held_until = time.time() + delay
            print(f"  HTTP {response.status_code} from {urlparse(url).netloc}, backing off {delay:.1f}s (still going)")
            if response.status_code == 429:
                notice = "Rekhta asked us to slow down. That's all right — retrying, and this download keeps going."
            else:
                notice = "Rekhta had a hiccup. That's all right — retrying, and this download keeps going."
            _notify(on_status, {"notice": notice, "mood": "", "retry_until": held_until})
            limiter.penalize(delay, slow=not raw_delay)
            continue
        if held_until:
            _notify(on_status, {"notice": "", "mood": "", "retry_until": 0})
            held_until = 0.0
        response.raise_for_status()
        return response


def slug_from_ebook_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def book_id_from_page(
    session: requests.Session,
    ebook_url: str,
    limiter: Optional[RateLimiter] = None,
    on_status: Optional[Callable[[dict], None]] = None,
) -> str:
    """bkId is embedded in the reader page. The address-bar URL is enough."""
    session.headers["Referer"] = ebook_url
    if limiter is None:
        response = session.get(ebook_url, timeout=60)
        response.raise_for_status()
    else:
        response = fetch(session, ebook_url, limiter, on_status)
    match = re.search(r"bkId=([0-9a-fA-F-]{36})", response.text)
    if not match:
        match = re.search(r'data-id="([0-9a-fA-F-]{36})"', response.text)
    if not match:
        raise RuntimeError(f"No book id on {ebook_url}")
    return match.group(1)


def fetch_page_catalog(
    session: requests.Session,
    limiter: RateLimiter,
    slug: str,
    limit: Optional[int] = None,
    on_status: Optional[Callable[[dict], None]] = None,
) -> tuple[list[str], list[str]]:
    """Ids arrive 40 at a time. Stop only when a batch is empty or shorter than 40."""
    ids: list[str] = []
    chunk = 40
    offset = 0
    while True:
        url = (
            f"{REKHTA_ORIGIN}/EbookData/GetEbookPageIds/"
            f"?slug={slug}&lang=1&from={offset}&count={chunk}"
        )
        batch = fetch(session, url, limiter, on_status).json().get("ids") or []
        if not batch:
            break
        ids.extend(batch)
        print(f"catalog {len(ids)} page ids (asked from {offset})")
        if len(batch) < chunk:
            break
        offset += len(batch)
        if limit is not None and len(ids) >= limit:
            ids = ids[:limit]
            break
    names = [f"{i + 1:03d}.jpg" for i in range(len(ids))]
    return names, ids


def fetch_layout(
    session: requests.Session,
    limiter: RateLimiter,
    bk_id: str,
    pg_idx: int,
    pg_id: str,
    on_status: Optional[Callable[[dict], None]] = None,
) -> dict:
    url = (
        f"{REKHTA_ORIGIN}/EbookData/GetEbookFromApi/"
        f"?bkId={bk_id}&pgIdx={pg_idx}&pgid={pg_id}"
    )
    return fetch(session, url, limiter, on_status).json()


def sprite_url(bk_id: str, page_image_name: str) -> str:
    name = page_image_name if page_image_name.endswith(".jpg") else f"{page_image_name}.jpg"
    return f"{TILE_CDN}/{bk_id}/{name}"


def stitch_page(layout: dict, sprite: Image.Image) -> Image.Image:
    page_w = int(layout["PageWidth"])
    page_h = int(layout["PageHeight"])
    grid_x = int(layout["X"])
    grid_y = int(layout["Y"])
    subs = layout.get("Sub") or []

    if page_w <= 0 or page_h <= 0:
        page_w = grid_x * SOURCE_TILE
        page_h = grid_y * SOURCE_TILE

    out = Image.new("RGB", (page_w, page_h), (255, 255, 255))

    for cell in subs:
        x1, y1 = int(cell["X1"]), int(cell["Y1"])
        x2, y2 = int(cell["X2"]), int(cell["Y2"])
        sx = x1 * SOURCE_STRIDE
        sy = y1 * SOURCE_STRIDE
        dx = x2 * SOURCE_TILE
        dy = y2 * SOURCE_TILE
        tile = sprite.crop((sx, sy, sx + SOURCE_TILE, sy + SOURCE_TILE))
        out.paste(tile, (dx, dy))

    return out


class FirstPageGate:
    """Let the first page finish its layout before any other page sends a request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._opened = False

    def lead(self) -> bool:
        self._lock.acquire()
        if self._opened:
            self._lock.release()
            return False
        return True

    def done(self) -> None:
        self._opened = True
        self._lock.release()


class SpritePrefetch:
    """Download tile JPGs alongside layouts, a couple at a time.

    Nothing starts until a page asks for its picture, so the opening of a book
    is not a burst of downloads.
    """

    def __init__(
        self,
        session: requests.Session,
        limiter: RateLimiter,
        bk_id: str,
        jobs: list[tuple[int, str, str]],
        on_status: Optional[Callable[[dict], None]] = None,
        ahead: int = 4,
    ):
        self.session = session
        self.limiter = limiter
        self.bk_id = bk_id
        self.on_status = on_status
        self.ahead = ahead
        self.waiting = [(pg_idx, img_name) for pg_idx, _pg_id, img_name in jobs]
        self.futures: dict[int, object] = {}
        self.executor = ThreadPoolExecutor(max_workers=ahead)
        self.lock = threading.Lock()

    def _download(self, img_name: str) -> bytes:
        return fetch(
            self.session,
            sprite_url(self.bk_id, img_name),
            self.limiter,
            self.on_status,
        ).content

    def _pump(self) -> None:
        while len(self.futures) < self.ahead and self.waiting:
            pg_idx, img_name = self.waiting.pop(0)
            self.futures[pg_idx] = self.executor.submit(self._download, img_name)

    def ensure(self, pg_idx: int):
        with self.lock:
            future = self.futures.get(pg_idx)
            if future is None:
                for index, (idx, img_name) in enumerate(self.waiting):
                    if idx == pg_idx:
                        self.waiting.pop(index)
                        future = self.executor.submit(self._download, img_name)
                        self.futures[pg_idx] = future
                        break
            if future is None:
                raise RuntimeError(f"No sprite queued for page {pg_idx}")
            return future

    def release(self, pg_idx: int) -> None:
        with self.lock:
            self.futures.pop(pg_idx, None)
            self._pump()

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)


def stitch_one(
    session: requests.Session,
    limiter: RateLimiter,
    bk_id: str,
    pg_idx: int,
    pg_id: str,
    img_name: str,
    output_dir: Path,
    prefetch: SpritePrefetch,
    gate: FirstPageGate,
    on_status: Optional[Callable[[dict], None]] = None,
) -> Path:
    print(f"Page {pg_idx}: layout pgid={pg_id[:8]}… sprite={img_name}")
    leading = gate.lead()
    sprite_future = None
    try:
        sprite_future = prefetch.ensure(pg_idx)
        layout = fetch_layout(session, limiter, bk_id, pg_idx, pg_id, on_status)
        sprite_bytes = sprite_future.result()
    finally:
        if leading:
            gate.done()
        if sprite_future is not None:
            prefetch.release(pg_idx)
    page_img = stitch_page(layout, Image.open(BytesIO(sprite_bytes)).convert("RGB"))
    out_path = output_dir / f"page_{pg_idx:03d}.png"
    # compress_level only changes zlib effort. Pixels stay identical, so the PDF does too.
    page_img.save(out_path, "PNG", compress_level=1)
    print(f"  -> {out_path.name} ({page_img.size[0]}x{page_img.size[1]})")
    return out_path


def save_pages(
    *,
    bk_id: str,
    slug: str,
    output_dir: Path,
    start_page: int = 1,
    end_page: Optional[int] = None,
    rate_per_sec: float = DEFAULT_RATE_PER_SEC,
    workers: int = DEFAULT_WORKERS,
    page_ids: Optional[list[str]] = None,
    page_names: Optional[list[str]] = None,
    on_status: Optional[Callable[[dict], None]] = None,
    session: Optional[requests.Session] = None,
    limiter: Optional[RateLimiter] = None,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if limiter is None:
        # Burst of 1: a full bucket fires the first catalog call immediately and Rekhta answers 429.
        limiter = RateLimiter(rate_per_sec, burst=1)
    # Tile JPGs are on ebooksapi, not the layout API that returned 429.
    image_limiter = RateLimiter(4.0, burst=1)
    if session is None:
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
    print(f"rate {limiter.rate:.2f} req/s, workers {workers}")

    if page_ids is None or page_names is None:
        print(f"pausing {FIRST_API_PAUSE:.0f}s before the page list")
        _notify(
            on_status,
            {
                "phase": "counting",
                "notice": "Off to a good start. The pages begin in a few seconds.",
                "mood": "calm",
            },
        )
        time.sleep(FIRST_API_PAUSE)
        _notify(on_status, {"notice": "", "mood": ""})
        page_names, page_ids = fetch_page_catalog(
            session, limiter, slug, limit=end_page, on_status=on_status
        )
    last_page = len(page_ids) if end_page is None else min(end_page, len(page_ids))
    total_pages = max(0, last_page - start_page + 1)
    print(f"document has {len(page_ids)} pages; stitching {start_page}–{last_page}")
    _notify(on_status, {"phase": "counting", "total": total_pages, "done": 0})

    jobs = []
    skipped = 0
    for pg_idx in range(start_page, last_page + 1):
        out_path = output_dir / f"page_{pg_idx:03d}.png"
        if out_path.exists() and out_path.stat().st_size > 0:
            print(f"skip {out_path.name}")
            skipped += 1
            continue
        jobs.append((pg_idx, page_ids[pg_idx - 1], page_names[pg_idx - 1]))
    print(f"stitching {len(jobs)} pages ({start_page}–{jobs[-1][0] if jobs else start_page})")
    _notify(
        on_status,
        {"phase": "stitching", "total": total_pages, "done": skipped, "notice": ""},
    )

    saved: list[Path] = []
    done = skipped
    image_session = requests.Session()
    image_session.headers.update(session.headers)
    prefetch = SpritePrefetch(
        image_session,
        image_limiter,
        bk_id,
        jobs,
        on_status=on_status,
        ahead=2,
    )
    gate = FirstPageGate()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    stitch_one,
                    session,
                    limiter,
                    bk_id,
                    pg_idx,
                    pg_id,
                    img_name,
                    output_dir,
                    prefetch,
                    gate,
                    on_status,
                )
                for pg_idx, pg_id, img_name in jobs
            ]
            for future in as_completed(futures):
                saved.append(future.result())
                done += 1
                _notify(
                    on_status,
                    {"phase": "stitching", "total": total_pages, "done": done, "notice": ""},
                )
    finally:
        prefetch.close()
    saved.sort()
    return saved


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stitch Rekhta ebook pages from API + tile JPGs")
    p.add_argument("--bk-id", default=DEFAULT_BK_ID)
    p.add_argument("--slug", default=DEFAULT_SLUG)
    p.add_argument("--ebook-url", default=None, help="Derive slug from Rekhta ebook URL")
    p.add_argument(
        "--pages",
        type=int,
        default=None,
        help="Optional last page. Default: keep going until the page-id list ends.",
    )
    p.add_argument("--start", type=int, default=1, help="First page number")
    p.add_argument("--output-dir", type=Path, default=Path("books"))
    p.add_argument("--rate", type=float, default=DEFAULT_RATE_PER_SEC, help="Requests per second (shared)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel page workers (2-4)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    slug = slug_from_ebook_url(args.ebook_url) if args.ebook_url else args.slug
    save_pages(
        bk_id=args.bk_id,
        slug=slug,
        output_dir=args.output_dir,
        start_page=args.start,
        end_page=args.pages,
        rate_per_sec=args.rate,
        workers=max(1, args.workers),
    )
    if args.pages is not None:
        print("PDF skipped: --pages stops before the end of the book.")
    else:
        from scripts.to_pdf import catalog_length, write_pdf

        total = catalog_length(slug)
        pdf_path = args.output_dir / f"{slug}.pdf"
        write_pdf(args.output_dir, pdf_path, total)
