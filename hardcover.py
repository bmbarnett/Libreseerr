"""Hardcover helpers: exact book matching for requests, search, and Discover rows.

Bookshelf/Readarr book IDs are Hardcover book IDs, and a text search against Bookshelf
often returns the wrong edition (graphic novel, dramatized audio, compilation, foreign
title). Hardcover's own search ranks the real book first by reader count, so we resolve
the request there and ask Bookshelf for exactly that ID.

Enabled only when the HARDCOVER_TOKEN environment variable is set. Every function fails
soft (returns None / []) so callers can fall back to their normal behaviour.
"""
import logging
import os
import re
import time
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.hardcover.app/v1/graphql"
CACHE_TTL = 6 * 3600

_SEARCH = (
    "query($q:String!,$n:Int!,$filter:String,$sort:String){ "
    "search(query:$q, query_type:\"Book\", per_page:$n, page:1, filter_by:$filter, sort:$sort){ results } }"
)
_TRENDING = "query($f:date!,$t:date!,$n:Int!){ books_trending(from:$f, to:$t, limit:$n){ ids } }"
_cache = {}


def enabled():
    return bool(os.environ.get("HARDCOVER_TOKEN"))


def _auth_header():
    token = os.environ.get("HARDCOVER_TOKEN", "").strip()
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def _gql(query, variables):
    """POST a GraphQL query, retrying once after a rate-limit response."""
    for attempt in range(2):
        resp = requests.post(
            API_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": _auth_header(), "User-Agent": "libreseerr"},
            timeout=25,
        )
        if resp.status_code == 429 and attempt == 0:
            time.sleep(5)
            continue
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(str(body["errors"])[:200])
        return body.get("data", {})
    raise RuntimeError("Hardcover rate limited")


def _norm(text):
    text = re.sub(r"[\(\[].*?[\)\]]", " ", (text or "").lower())
    text = re.sub(r"\s*[:\-]\s*an?\s+(novel|memoir|thriller|mystery|romance|story|tale|biography)\s*$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def norm_title(text):
    return _norm(text)


def _author_matches(wanted, names):
    """True if any Hardcover author name shares a name part with the requested author."""
    want = {w for w in re.split(r"\W+", (wanted or "").lower()) if len(w) > 1}
    for name in names or []:
        have = {w for w in re.split(r"\W+", name.lower()) if len(w) > 1}
        if want & have:
            return True
    return False


def to_book(doc):
    """Convert a Hardcover search document to the book shape the UI uses."""
    isbns = doc.get("isbns") or []
    image = doc.get("image") or {}
    year = doc.get("release_year")
    return {
        "id": f"hc-{doc.get('id')}",
        "hardcover_id": str(doc.get("id")),
        "title": doc.get("title", "Unknown"),
        "authors": doc.get("author_names") or [],
        "publishedDate": str(year) if year else "",
        "description": "",
        "pageCount": doc.get("pages") or 0,
        "categories": (doc.get("genres") or [])[:5],
        "isbn_13": next((i for i in isbns if len(i) == 13), ""),
        "isbn_10": next((i for i in isbns if len(i) == 10), ""),
        "cover": image.get("url", "") if isinstance(image, dict) else "",
        "language": "en",
        "users": doc.get("users_count") or 0,
    }


def _search(query, per_page=10, filter_by=None, sort=None, use_cache=True):
    key = (_norm(query), per_page, filter_by, sort)
    hit = _cache.get(key)
    if use_cache and hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    data = _gql(_SEARCH, {"q": query, "n": per_page, "filter": filter_by, "sort": sort})
    results = (data.get("search") or {}).get("results") or {}
    docs = [h.get("document", {}) for h in results.get("hits", [])]
    _cache[key] = (time.time(), docs)
    return docs


def find_book(title, author):
    """Return the best Hardcover match as {"id","title","author","users","book"} or None.

    Candidates are ranked: not a compilation, exact title, author match, then most readers.
    A title that isn't at least a prefix match is rejected.
    """
    if not enabled() or not title:
        return None
    try:
        docs = _search(f"{title} {author or ''}".strip())
    except Exception as exc:
        logger.warning("Hardcover search failed for '%s': %s", title, exc)
        return None

    want = _norm(title)
    ranked = []
    for d in docs:
        t = _norm(d.get("title", ""))
        if not t or not (t == want or t.startswith(want) or want.startswith(t)):
            continue
        ranked.append((
            bool(d.get("compilation")),
            t != want,
            not _author_matches(author, d.get("author_names")),
            -(d.get("users_count") or 0),
            d,
        ))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[:4])
    best = ranked[0][4]
    # A different-author match is only trusted for an exact title (Hardcover sometimes credits
    # the wrong contributor, e.g. A Christmas Carol), and only if it is clearly the popular one.
    if ranked[0][2] and (ranked[0][1] or (best.get("users_count") or 0) < 50):
        return None
    book = to_book(best)
    return {
        "id": book["hardcover_id"],
        "title": book["title"],
        "author": (book["authors"] or [""])[0],
        "users": book["users"],
        "book": book,
    }


def search_books(query, per_page=20):
    """Search Hardcover for the UI search box. Returns a list of UI book dicts (compilations last)."""
    if not enabled() or not query:
        return []
    try:
        docs = _search(query, per_page=per_page, filter_by="compilation:=false", use_cache=False)
    except Exception as exc:
        logger.warning("Hardcover search failed for '%s': %s", query, exc)
        return []
    return [to_book(d) for d in docs]


def list_books(filter_by, sort="users_count:desc", per_page=20):
    """Return UI book dicts for a filtered/sorted Hardcover list (Discover rows)."""
    docs = _search("*", per_page=per_page, filter_by=filter_by, sort=sort, use_cache=False)
    return [to_book(d) for d in docs]


def trending(days=30, limit=20):
    """Return UI book dicts for books Hardcover reports as trending over the last `days`."""
    today = date.today()
    data = _gql(_TRENDING, {"f": str(today - timedelta(days=days)), "t": str(today), "n": limit * 2})
    ids = ((data.get("books_trending") or {}).get("ids")) or []
    if not ids:
        return []
    docs = _search("*", per_page=min(len(ids), 100),
                   filter_by="id:=[" + ",".join(str(i) for i in ids) + "] && compilation:=false",
                   use_cache=False)
    order = {str(i): n for n, i in enumerate(ids)}
    docs.sort(key=lambda d: order.get(str(d.get("id")), 999))
    return [to_book(d) for d in docs][:limit]
