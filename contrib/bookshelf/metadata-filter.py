#!/usr/bin/env python3
"""Filtering proxy between Bookshelf and the rreading-glasses metadata server.

Bookshelf asks the metadata server for an author and imports every work in the answer (hundreds of
editions, study guides, foreign titles). This proxy returns only the author's real books, chosen
by catalogue.select_books (Hardcover readers cross-checked with Open Library), adds real books the
metadata server left out, and always keeps books you already have files for. Everything else (works,
searches, covers) passes straight through. If anything goes wrong it returns the unfiltered answer.

Environment: UPSTREAM (default http://rreading-glasses:8788), HARDCOVER_TOKEN, and for the owned-books
check EBOOK_URL/EBOOK_API_KEY and AUDIO_URL/AUDIO_API_KEY. PORT (default 8788), CACHE_HOURS (default 6).
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import catalogue

UPSTREAM = os.environ.get("UPSTREAM", "http://rreading-glasses:8788").rstrip("/")
PORT = int(os.environ.get("PORT", "8788"))
CACHE_SECONDS = float(os.environ.get("CACHE_HOURS", "6")) * 3600
BOOKSHELFS = [(os.environ.get("EBOOK_URL"), os.environ.get("EBOOK_API_KEY")),
              (os.environ.get("AUDIO_URL"), os.environ.get("AUDIO_API_KEY"))]
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "etag",
              "last-modified", "date", "server"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("metadata-filter")
cache = {}                      # author id -> (time, body bytes)
locks = {}                      # author id -> Lock, so two requests don't build the same author twice
locks_guard = threading.Lock()


def upstream_get(path, timeout=120):
    req = urllib.request.Request(UPSTREAM + path, headers={"User-Agent": "metadata-filter"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", "application/json"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "application/json"), e.read()


def bookshelf_api(base, key, path):
    req = urllib.request.Request(base.rstrip("/") + "/api/v1" + path, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def owned_work_ids(foreign_author_id):
    """Hardcover ids of this author's books that have a file in any Bookshelf instance.

    Raises if a Bookshelf instance cannot be asked, so the caller falls back to the unfiltered answer
    rather than risk dropping a book you own."""
    owned = set()
    for base, key in BOOKSHELFS:
        if not base or not key:
            continue
        for author in bookshelf_api(base, key, "/author"):
            if str(author.get("foreignAuthorId")) != str(foreign_author_id):
                continue
            for b in bookshelf_api(base, key, f"/book?authorId={author['id']}"):
                if (b.get("statistics") or {}).get("bookFileCount"):
                    owned.add(int(b["foreignBookId"]))
    return owned


def filter_author(raw):
    """Return the filtered author document as bytes, or None to pass the original through."""
    doc = json.loads(raw)
    works = doc.get("Works") or []
    name = doc.get("Name") or ""
    if not works or not name:
        return None
    chosen = catalogue.select_books(name)
    if not chosen or not chosen["keep"]:
        log.info("%s: no catalogue data, passing through", name)
        return None
    keep_ids = {b["id"] for b in chosen["keep"]}
    owned = owned_work_ids(doc.get("ForeignId"))
    wanted = keep_ids | owned

    have = {int(w["ForeignId"]) for w in works}
    kept = [w for w in works if int(w["ForeignId"]) in wanted]
    added = 0
    for work_id in sorted(keep_ids - have):
        status, _, body = upstream_get(f"/work/{work_id}", timeout=60)
        if status == 200:
            try:
                kept.append(json.loads(body))
                added += 1
            except ValueError:
                pass
    kept_ids = {int(w["ForeignId"]) for w in kept}

    # Series only reference works that are still present
    series = []
    for s in doc.get("Series") or []:
        items = [i for i in s.get("LinkItems") or [] if int(i.get("ForeignWorkId", 0)) in kept_ids]
        if items:
            series.append({**s, "LinkItems": items})
    doc["Works"], doc["Series"] = kept, series
    log.info("%s: %d works -> %d (+%d added, %d owned kept)", name, len(works), len(kept), added, len(owned))
    return json.dumps(doc).encode()


def author_response(author_id, query):
    now = time.time()
    hit = cache.get(author_id)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    with locks_guard:
        lock = locks.setdefault(author_id, threading.Lock())
    with lock:
        hit = cache.get(author_id)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        status, ctype, raw = upstream_get(f"/author/{author_id}{query}", timeout=180)
        if status != 200:
            return None
        try:
            body = filter_author(raw)
        except Exception as exc:
            log.warning("author %s: filtering failed (%s), passing through", author_id, exc)
            body = None
        if body is None:
            return raw          # not cached, so the next request tries again
        cache[author_id] = (time.time(), body)
        return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, ctype, body, head=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_GET(self, head=False):
        try:
            m = re.match(r"^/author/(\d+)(\?.*)?$", self.path)
            if m:
                body = author_response(m.group(1), m.group(2) or "")
                if body is not None:
                    return self._send(200, "application/json", body, head)
            status, ctype, body = upstream_get(self.path)
            self._send(status, ctype, body, head)
        except Exception as exc:
            log.warning("%s failed: %s", self.path, exc)
            self._send(502, "text/plain", b"upstream error", head)

    def do_HEAD(self):
        self.do_GET(head=True)


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    log.info("metadata-filter on :%d -> %s", PORT, UPSTREAM)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
