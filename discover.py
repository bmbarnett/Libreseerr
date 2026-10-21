"""Discover rows built from Hardcover (trending, genres) and the NYT best-seller lists.

Rows are rebuilt in the background every few hours and served from memory (and a JSON file
so they survive restarts), so the page loads instantly and the lists stay fresh.
Each row is skipped if its data source isn't configured or fails.
"""
import json
import logging
import os
import threading
import time
from datetime import date

import requests

import hardcover

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "discover.json")
REFRESH_SECONDS = 3 * 3600
NYT_LISTS = [
    ("nyt_fiction", "NYT Best Sellers: Fiction", "combined-print-and-e-book-fiction"),
    ("nyt_nonfiction", "NYT Best Sellers: Nonfiction", "combined-print-and-e-book-nonfiction"),
]
GENRES = [
    ("fantasy", "Fantasy", "Fantasy"),
    ("science_fiction", "Science Fiction", "Science Fiction"),
    ("mystery", "Mystery & Thriller", "Mystery"),
    ("romance", "Romance", "Romance"),
    ("horror", "Horror", "Horror"),
    ("young_adult", "Young Adult", "Young Adult"),
    ("historical", "Historical Fiction", "Historical Fiction"),
    ("nonfiction", "Non-Fiction", "Nonfiction"),
]

_rows = {}  # key -> {"title", "books", "updated"}
_order = []
_lock = threading.Lock()


def nyt_enabled():
    return bool(os.environ.get("NYT_API_KEY"))


def _build_nyt(list_name):
    resp = requests.get(
        f"https://api.nytimes.com/svc/books/v3/lists/current/{list_name}.json",
        params={"api-key": os.environ["NYT_API_KEY"]},
        timeout=20,
    )
    resp.raise_for_status()
    books = []
    for item in resp.json().get("results", {}).get("books", [])[:20]:
        title = (item.get("title") or "").title()
        author = item.get("author") or ""
        hit = hardcover.find_book(title, author) if hardcover.enabled() else None
        if hit:
            book = hit["book"]
        else:
            book = {
                "id": f"nyt-{item.get('primary_isbn13')}",
                "hardcover_id": "",
                "title": title,
                "authors": [author] if author else [],
                "publishedDate": "",
                "description": item.get("description", ""),
                "pageCount": 0,
                "categories": [],
                "isbn_13": item.get("primary_isbn13", ""),
                "isbn_10": item.get("primary_isbn10", ""),
                "cover": item.get("book_image", ""),
                "language": "en",
            }
        book["rank"] = item.get("rank")
        books.append(book)
        time.sleep(0.5)  # stay well under Hardcover's rate limit
    return books


def _definitions():
    """Ordered (key, title, builder) tuples for every row that can be built."""
    year = date.today().year
    defs = []
    if nyt_enabled():
        for key, title, name in NYT_LISTS:
            defs.append((key, title, lambda n=name: _build_nyt(n)))
    if hardcover.enabled():
        defs.append(("trending", "Trending Now", lambda: hardcover.trending(days=30, limit=20)))
        defs.append(("new_releases", "New & Popular Releases", lambda: hardcover.list_books(
            f"release_year:>={year} && compilation:=false && users_count:>=300")))
        for key, title, genre in GENRES:
            defs.append((key, title, lambda g=genre: hardcover.list_books(
                f"genres:=[`{g}`] && release_year:>={year - 3} && compilation:=false && users_count:>=300")))
        defs.append(("classics", "Classics", lambda: hardcover.list_books(
            "genres:=[`Classics`] && compilation:=false && users_count:>=2000")))
    return defs


def refresh():
    """Rebuild every row; a failed row keeps its previous contents."""
    defs = _definitions()
    seen = set()  # a book shows once across the new-release and genre rows
    for key, title, builder in defs:
        try:
            books = builder()
            if key not in ("trending",) and not key.startswith("nyt_"):
                books = [b for b in books if b.get("hardcover_id") not in seen]
                seen.update(b.get("hardcover_id") for b in books)
            if books:
                with _lock:
                    _rows[key] = {"title": title, "books": books, "updated": time.time()}
        except Exception as exc:
            logger.warning("Discover row '%s' failed: %s", key, exc)
        time.sleep(1.5)
    with _lock:
        _order[:] = [k for k, _, _ in defs if k in _rows]
    _save()
    logger.info("Discover refreshed: %d rows", len(_order))


def _save():
    try:
        with _lock:
            data = {"rows": _rows, "order": list(_order)}
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
        global _loaded_mtime
        _loaded_mtime = os.path.getmtime(CACHE_FILE)
    except OSError as exc:
        logger.warning("Could not save discover cache: %s", exc)


_loaded_mtime = 0


def _load():
    """(Re)load the cache file if another worker has written a newer one."""
    global _loaded_mtime
    try:
        mtime = os.path.getmtime(CACHE_FILE)
        if mtime == _loaded_mtime:
            return
        with open(CACHE_FILE) as f:
            data = json.load(f)
        with _lock:
            _rows.clear()
            _rows.update(data.get("rows", {}))
            _order[:] = data.get("order", [])
        _loaded_mtime = mtime
    except (OSError, ValueError):
        pass


def row_list():
    """[{key, title}] for the rows currently available, in display order."""
    _load()
    with _lock:
        return [{"key": k, "title": _rows[k]["title"]} for k in _order if k in _rows]


def row_books(key):
    _load()
    with _lock:
        row = _rows.get(key)
        return list(row["books"]) if row else None


def start_loop():
    """Start the background refresh loop (call from the leader worker only)."""
    def loop():
        time.sleep(5)
        while True:
            try:
                refresh()
            except Exception as exc:
                logger.warning("Discover refresh failed: %s", exc)
            time.sleep(REFRESH_SECONDS)

    threading.Thread(target=loop, daemon=True, name="discover-refresh").start()
