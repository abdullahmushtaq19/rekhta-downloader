# Rekhta Downloader

Downloads Rekhta books as PDFs, without the Rekhta watermark.

You paste a book link. The downloader saves the PDF on your computer.

## 1. Clone

```
git clone https://github.com/abdullahmushtaq19/rekhta-downloader.git
cd rekhta-downloader
```

Stay in that folder for the steps below.

## 2. First time only

Do this once after cloning. These commands install the dependencies.

**Windows (PowerShell)**

```
py -3 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

**macOS and Linux**

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You do not run these three lines again.

## 3. Every time you want a book

Open a terminal in this folder. Activate first, then start the downloader. Both lines, every new terminal.

**Windows (PowerShell)**

```
.\.venv\Scripts\activate
python rekhta_downloader.py
```

**macOS and Linux**

```
source .venv/bin/activate
python rekhta_downloader.py
```

Leave that window open. Your browser opens the downloader.

1. On Rekhta, open a book and copy the address-bar URL. It looks like `https://www.rekhta.org/ebooks/some-book-name-ebooks`.
2. Paste it and press **Download**.
3. **+ Add another** adds another link. Empty boxes are ignored.
4. **Download** queues that one link. **Download all** queues every filled link.
5. Books run one at a time. The bar shows how many pages are saved.
6. If Rekhta hits a limit, don't worry. That happens, and it is fine. A countdown shows the seconds left, and the book keeps downloading.
7. When a book finishes, the row says the PDF is ready.

You can close the browser tab. Keep the terminal window open until the queue is done.

The PDF is saved here:

```
books/<book-name>/<book-name>.pdf
```

That folder is created on the first download.

If a download stops early, do step 3 again and paste the same link. Pages already saved are skipped. The PDF is written when the book is complete.

## One book from the terminal

Open `scripts/run_book.py`, set `BOOK_URL` to the address-bar link, activate as in step 3, then run:

```
python scripts/run_book.py
```

Made by Abdullah Mushtaq.
