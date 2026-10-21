"""Optional Hardcover lookup used to pick the exact book for a request.

Bookshelf/Readarr book IDs are Hardcover book IDs, and a text search against Bookshelf
often returns the wrong edition (graphic novel, dramatized audio, compilation, foreign
title). Hardcover's own search ranks the real book first by reader count, so we resolve
the request there and ask Bookshelf for exactly that ID.

Enabled only when the HARDCOVER_TOKEN environment variable is set. Every function fails
soft (returns None) so callers can fall back to their normal search.
"""
import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.hardcover.app/v1/graphql"
CACHE_TTL = 6 * 3600

_SEARCH = (
    "query($q:String!){ search(query:$q, query_type:\"Book\", per_page:10, page:1){ results } }"
)
_cache = {}


def enabled():
    return bool(os.environ.get("HARDCOVER_TOKEN"))


def _auth_header():
    token = os.environ.get("HARDCOVER_TOKEN", "").strip()
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def _norm(text):
    text = re.sub(r"[\(\[].*?[\)\]]", " ", (text or "").lower())
    text = re.sub(r"\s*[:\-]\s*an?\s+(novel|memoir|thriller|mystery|romance|story|tale|biography)\s*$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def _author_matches(wanted, names):
    """True if any Hardcover author name shares a surname with the requested author."""
    want = {w for w in re.split(r"\W+", (wanted or "").lower()) if len(w) > 1}
    for name in names or []:
        have = {w for w in re.split(r"\W+", name.lower()) if len(w) > 1}
        if want & have:
            return True
    return False


def _search(query):
    key = _norm(query)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    resp = requests.post(
        API_URL,
        json={"query": _SEARCH, "variables": {"q": query}},
        headers={"Authorization": _auth_header(), "User-Agent": "libreseerr"},
        timeout=20,
    )
    resp.raise_for_status()
    results = (resp.json().get("data", {}).get("search", {}) or {}).get("results") or {}
    docs = [h.get("document", {}) for h in results.get("hits", [])]
    _cache[key] = (time.time(), docs)
    return docs


def find_book(title, author):
    """Return the best Hardcover match as {"id", "title", "author", "users"} or None.

    Candidates are ranked: not a compilation, exact title, title prefix, author match,
    then most readers. A title that isn't at least a prefix match is rejected.
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
    return {
        "id": str(best.get("id")),
        "title": best.get("title", ""),
        "author": (best.get("author_names") or [""])[0],
        "users": best.get("users_count") or 0,
    }
