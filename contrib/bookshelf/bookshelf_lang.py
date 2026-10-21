"""Find non-English books among Bookshelf books using Hardcover (Bookshelf book ids are Hardcover ids).

Shared by author-backfill.py and wanted-cleanup.py. The Hardcover token is read from the abs-agg
container's environment (HARDCOVER_TOKEN), so nothing is stored here.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

API_URL = "https://api.hardcover.app/v1/graphql"
QUERY = (
    "query($ids:[Int!]){ books(where:{id:{_in:$ids}}){ id title "
    "default_physical_edition{language{language}} default_ebook_edition{language{language}} "
    "default_cover_edition{language{language}} default_audio_edition{language{language}} } }"
)
EDITION_KEYS = ("default_physical_edition", "default_ebook_edition", "default_cover_edition", "default_audio_edition")
FOREIGN_WORDS = {"y", "el", "la", "los", "las", "del", "und", "der", "das", "les", "des", "une", "et",
                 "gli", "uma", "och", "het", "een"}
ENGLISH_WORDS = {"the", "and", "of", "to", "with", "for", "is", "from"}
# Wider set used to rescue English-titled books whose default Hardcover edition happens to be foreign
ENGLISH_COMMON = ENGLISH_WORDS | {"a", "an", "in", "on", "at", "by", "or", "my", "his", "her", "you", "it", "all"}

_token = None


def _auth():
    global _token
    if _token is None:
        tok = os.environ.get("HARDCOVER_TOKEN", "").strip()     # set inside the metadata-filter container
        if not tok:
            env = subprocess.check_output(
                ["docker", "inspect", "abs-agg", "--format", "{{range .Config.Env}}{{println .}}{{end}}"], text=True)
            tok = next(l.split("=", 1)[1] for l in env.splitlines() if l.startswith("HARDCOVER_TOKEN="))
        _token = tok if tok.lower().startswith("bearer ") else "Bearer " + tok
    return _token


_last_call = 0.0
_throttle_lock = threading.Lock()


_blocked_until = 0.0


class QuotaExhausted(Exception):
    """Hardcover's daily allowance (5,000 requests on the free plan) is used up; stop calling until it resets."""


def note_rate_limit(err):
    """After an HTTP 429: if the daily allowance is gone (or Hardcover says to wait a long time), block calls."""
    global _blocked_until
    try:
        remaining = err.headers.get("x-ratelimit-daily-remaining")
        retry = int(err.headers.get("retry-after") or 0)
    except (AttributeError, ValueError):
        return
    if remaining == "0" or retry > 120:
        _blocked_until = time.time() + min(max(retry, 300), 86400)


def throttle(min_gap=2.0):
    """Space out Hardcover calls: one token is shared by several services and Hardcover allows ~60/minute."""
    global _last_call
    if time.time() < _blocked_until:
        raise QuotaExhausted(f"Hardcover daily quota used up, retry in {int(_blocked_until - time.time())}s")
    with _throttle_lock:
        wait = _last_call + min_gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def _query(ids):
    body = json.dumps({"query": QUERY, "variables": {"ids": ids}}).encode()
    for attempt in range(5):
        throttle()
        req = urllib.request.Request(API_URL, data=body, headers={
            "Authorization": _auth(), "Content-Type": "application/json", "User-Agent": "homelab-bookshelf"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["data"]["books"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                note_rate_limit(e)
                if time.time() < _blocked_until:
                    raise QuotaExhausted("Hardcover daily quota used up")
                if attempt < 4:
                    time.sleep(15 * (attempt + 1))      # per-minute limit: back off 15, 30, 45, 60 s
                    continue
            raise


def _words(title):
    return re.findall(r"[a-zà-ÿ']+", title.lower())


def _title_foreign(title, strong_only):
    if not strong_only and any(c.isalpha() and ord(c) > 127 for c in title):
        return True
    words = _words(title)
    count = sum(w in FOREIGN_WORDS for w in words)
    return not any(w in ENGLISH_WORDS for w in words) and (count >= 2 or (not strong_only and count >= 1))


def foreign_ids(books):
    """Return {book id: reason} for books that are not English.

    A book is kept if any edition is English. If Hardcover knows only other languages it is foreign.
    If it knows no language, the Hardcover and Bookshelf titles are checked for foreign words/letters.
    """
    by_hc = {}
    for b in books:
        try:
            by_hc[int(b["foreignBookId"])] = b
        except (KeyError, TypeError, ValueError):
            continue
    result = {}
    ids = list(by_hc)
    for i in range(0, len(ids), 100):
        for hc in _query(ids[i:i + 100]):
            book = by_hc.get(int(hc["id"]))
            if not book:
                continue
            langs = [((hc.get(k) or {}).get("language") or {}).get("language") or "" for k in EDITION_KEYS]
            langs = [l for l in langs if l]
            titles = [book.get("title", ""), hc.get("title", "")]
            if any(l.startswith("English") for l in langs):
                # Mislabelled editions: only a strong foreign-title signal overrides an English label
                # Judge by Hardcover's own title: Bookshelf shows an arbitrary (often foreign) edition title
                if _title_foreign(hc.get("title", ""), True):
                    result[book["id"]] = "foreign title"
            elif langs:
                # Foreign-only editions, unless the title is plainly English (e.g. "A Trail of Fire")
                plain_english = (all(ord(c) < 128 for c in book.get("title", ""))
                                 and sum(w in ENGLISH_COMMON for w in _words(book.get("title", ""))) >= 2
                                 and not _title_foreign(book.get("title", ""), True))
                if not plain_english:
                    result[book["id"]] = "/".join(sorted(set(langs)))[:30]
            elif any(_title_foreign(t, False) for t in titles):
                result[book["id"]] = "foreign title"
        time.sleep(1.5)
    return result
