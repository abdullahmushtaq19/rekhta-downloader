"""
Open a local download page in the browser.

    python rekhta_downloader.py

Paste Rekhta ebook links, queue one or all of them, and leave this window open.
PDFs are written to the books folder.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from scripts.run_book import delete_page_pngs
from scripts.stitch_ebook import (
    BROWSER_HEADERS,
    DEFAULT_WORKERS,
    RateLimiter,
    book_id_from_page,
    save_pages,
    slug_from_ebook_url,
)
from scripts.to_pdf import write_pdf

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "scripts" / "page.html"
BOOKS = ROOT / "books"
HOST = "127.0.0.1"
FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect width="32" height="32" rx="8" fill="#1b4332"/>'
    b'<path fill="#f4efe6" d="M8 7.5h11.2c1.8 0 3.3 1.5 3.3 3.3V25H11.2C9.4 25 8 23.6 8 21.8V7.5z"/>'
    b'<path fill="#1b4332" d="M11 11.2h8.2v1.4H11zm0 3.2h8.2v1.4H11zm0 3.2h5.4v1.4H11z"/>'
    b"</svg>"
)

# One pace for the whole queue. A new bucket per book starts full, so the first
# catalog call of every book was instant and Rekhta answered with Retry-After: 60.
API_LIMITER = RateLimiter(1.0, burst=1)

LOCK = threading.Lock()
JOBS: list[dict] = []
JOBS_BY_ID: dict[str, dict] = {}
WORK: queue.Queue[str] = queue.Queue()

PUBLIC_FIELDS = (
    "id",
    "url",
    "name",
    "slug",
    "state",
    "phase",
    "done",
    "total",
    "notice",
    "mood",
    "retry_until",
    "error",
    "pdf_url",
    "size_label",
    "created",
)


def canonical_ebook_url(text: str) -> str:
    raw = text.strip()
    if not raw:
        raise ValueError("Add a book link first.")
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host != "rekhta.org":
        raise ValueError("Use a link from rekhta.org/ebooks/…")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "ebooks":
        raise ValueError("Use the ebook address, the one that ends with the book name.")
    slug = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9-]+", slug):
        raise ValueError("That link does not look like a Rekhta ebook.")
    return f"https://www.rekhta.org/ebooks/{slug}"


def display_name(slug: str) -> str:
    text = slug[:-7] if slug.endswith("-ebooks") else slug
    return text.replace("-", " ")


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"Rekhta returned {exc.response.status_code} for this link."
    if isinstance(exc, requests.RequestException):
        return "Could not reach Rekhta. Check the connection and try this link again."
    text = str(exc).strip()
    if text.lower().startswith("no book id"):
        return "That page has no book id. Open the book on Rekhta and copy the address again."
    return text or "This download stopped."


def new_job(url: str, *, state: str, error: str = "") -> dict:
    slug = ""
    name = "Unusable link"
    if state != "error":
        slug = slug_from_ebook_url(url)
        name = display_name(slug)
    return {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "name": name,
        "slug": slug,
        "state": state,
        "phase": "error" if state == "error" else "queued",
        "done": 0,
        "total": 0,
        "notice": "",
        "mood": "",
        "retry_until": 0,
        "error": error,
        "pdf_url": "",
        "pdf_path": "",
        "size_label": "",
        "created": time.time(),
    }


def public_job(job: dict) -> dict:
    return {key: job.get(key, "") for key in PUBLIC_FIELDS}


def snapshot() -> list[dict]:
    with LOCK:
        return [public_job(job) for job in JOBS]


def update(job_id: str, **fields) -> None:
    with LOCK:
        job = JOBS_BY_ID.get(job_id)
        if job is not None:
            job.update(fields)


def enqueue(urls: list[str]) -> dict:
    added = 0
    duplicates = 0
    rejected: list[dict] = []
    ignored_empty = 0
    with LOCK:
        for raw in urls:
            text = (raw or "").strip()
            if not text:
                ignored_empty += 1
                continue
            try:
                url = canonical_ebook_url(text)
            except ValueError as exc:
                job = new_job(text, state="error", error=str(exc))
                JOBS.append(job)
                JOBS_BY_ID[job["id"]] = job
                rejected.append({"url": text, "error": str(exc)})
                continue
            if any(item["url"] == url and item["state"] in ("queued", "running") for item in JOBS):
                duplicates += 1
                continue
            job = new_job(url, state="queued")
            JOBS.append(job)
            JOBS_BY_ID[job["id"]] = job
            WORK.put(job["id"])
            added += 1
            print(f"queued {job['name']}")
    return {
        "added": added,
        "duplicates": duplicates,
        "rejected": rejected,
        "ignored_empty": ignored_empty,
    }


def run_job(job_id: str) -> None:
    with LOCK:
        job = JOBS_BY_ID.get(job_id)
        if job is None or job["state"] != "queued":
            return
        job["state"] = "running"
        job["phase"] = "opening"
        job["notice"] = ""
        job["mood"] = ""
        job["retry_until"] = 0
        job["error"] = ""
        url = job["url"]
        slug = job["slug"]
    print(f"starting {slug}")
    try:
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        session.headers["Referer"] = url

        def on_status(payload: dict) -> None:
            with LOCK:
                job = JOBS_BY_ID.get(job_id)
                if job is None:
                    return
                fields = dict(payload)
                # A page that just succeeded must not wipe a countdown another request is still inside.
                if fields.get("retry_until") == 0 and float(job.get("retry_until") or 0) > time.time() + 0.5:
                    fields.pop("retry_until", None)
                    if fields.get("notice") == "":
                        fields.pop("notice", None)
                        fields.pop("mood", None)
                job.update(fields)

        bk_id = book_id_from_page(session, url, API_LIMITER, on_status)
        output_dir = BOOKS / slug
        update(job_id, phase="counting")
        save_pages(
            bk_id=bk_id,
            slug=slug,
            output_dir=output_dir,
            start_page=1,
            end_page=None,
            workers=DEFAULT_WORKERS,
            on_status=on_status,
            session=session,
            limiter=API_LIMITER,
        )
        with LOCK:
            total = int(JOBS_BY_ID[job_id]["total"])
        if total <= 0:
            raise RuntimeError("This link did not return any pages.")
        update(job_id, phase="pdf", notice="", mood="", retry_until=0)
        pdf_path = output_dir / f"{slug}.pdf"
        write_pdf(output_dir, pdf_path, total)
        update(job_id, phase="cleanup")
        delete_page_pngs(output_dir, total)
        update(
            job_id,
            state="done",
            phase="done",
            notice="",
            mood="",
            retry_until=0,
            error="",
            pdf_url=f"/files/{slug}/{slug}.pdf",
            pdf_path=str(pdf_path.resolve()),
            size_label=format_size(pdf_path.stat().st_size),
        )
        print(f"PDF ready: {pdf_path}")
    except (Exception, SystemExit) as exc:
        message = friendly_error(exc)
        update(job_id, state="error", phase="error", error=message, notice="", mood="", retry_until=0)
        print(f"failed {slug}: {message}")


def worker() -> None:
    while True:
        job_id = WORK.get()
        try:
            run_job(job_id)
        finally:
            WORK.task_done()


def reveal(job_id: str) -> None:
    with LOCK:
        job = JOBS_BY_ID.get(job_id)
        if job is None or not job.get("pdf_path"):
            raise FileNotFoundError("That PDF is not ready.")
        target = Path(job["pdf_path"]).resolve()
    books = BOOKS.resolve()
    if target != books and books not in target.parents:
        raise FileNotFoundError("That PDF is not in the books folder.")
    if not target.is_file():
        raise FileNotFoundError("That PDF is no longer there.")
    subprocess.Popen(["explorer", f"/select,{target}"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        request = args[0] if args else ""
        if isinstance(request, str) and request.startswith("GET /api/jobs"):
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
            return
        if path == "/api/jobs":
            self._send_json({"jobs": snapshot()})
            return
        if path in ("/favicon.ico", "/favicon.svg"):
            self._send(200, "image/svg+xml", FAVICON)
            return
        file_match = re.fullmatch(r"/files/([A-Za-z0-9-]+)/\1\.pdf", path)
        if file_match:
            self._send_pdf(file_match.group(1))
            return
        self._send_json({"error": "Not found."}, 404)

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1_000_000:
            self._send_json({"error": "That request is too large."}, 413)
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "The page sent something this downloader could not read."}, 400)
            return
        if path == "/api/jobs":
            urls = payload.get("urls")
            if not isinstance(urls, list) or any(not isinstance(item, str) for item in urls):
                self._send_json({"error": "Send a list of links."}, 400)
                return
            result = enqueue(urls)
            result["jobs"] = snapshot()
            self._send_json(result)
            return
        if path == "/api/reveal":
            job_id = payload.get("id")
            if not isinstance(job_id, str):
                self._send_json({"error": "Missing book."}, 400)
                return
            try:
                reveal(job_id)
            except FileNotFoundError as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            except OSError:
                self._send_json({"error": "Could not open the books folder."}, 500)
                return
            self._send_json({"ok": True})
            return
        self._send_json({"error": "Not found."}, 404)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def _send_pdf(self, slug: str) -> None:
        path = (BOOKS / slug / f"{slug}.pdf").resolve()
        books = BOOKS.resolve()
        if books not in path.parents or not path.is_file():
            self._send_json({"error": "That PDF is not ready."}, 404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


def serve() -> None:
    os.chdir(ROOT)
    threading.Thread(target=worker, name="download", daemon=True).start()
    httpd = None
    port = 0
    for candidate in range(8765, 8775):
        try:
            httpd = ThreadingHTTPServer((HOST, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("No free local port between 8765 and 8774.")
    url = f"http://{HOST}:{port}"
    print(f"Rekhta Downloader: {url}", flush=True)
    print("Leave this window open. PDFs land in the books folder.", flush=True)
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. A book that was still running keeps its page images for the next start.")
        sys.exit(0)


if __name__ == "__main__":
    serve()
