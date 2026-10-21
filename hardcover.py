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
    text = re.sub(r"(sorcerer|philosopher) ?s stone", "philosophers stone", text)
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


_LANG_QUERY = (
    "query($ids:[Int!]){ books(where:{id:{_in:$ids}}){ id "
    "default_physical_edition{language{language}} default_ebook_edition{language{language}} "
    "default_cover_edition{language{language}} default_audio_edition{language{language}} } }"
)
_EDITION_KEYS = ("default_physical_edition", "default_ebook_edition", "default_cover_edition", "default_audio_edition")
_lang_cache = {}  # book id -> True (English) / False (other language) / None (unknown)
_FOREIGN_WORDS = {"y", "el", "la", "los", "las", "del", "und", "der", "die", "das", "les", "des", "une", "et",
                  "il", "lo", "gli", "um", "uma", "na", "ve", "och", "en", "het", "een", "van"}
_ENGLISH_WORDS = {"the", "and", "of", "to", "with", "for", "is", "from"}


def _looks_foreign(title):
    """Fallback when Hardcover has no language for a book: non-English letters or foreign function words."""
    if any(c.isalpha() and ord(c) > 127 for c in title):
        return True
    words = re.findall(r"[a-zà-ÿ']+", title.lower())
    return bool(words) and any(w in _FOREIGN_WORDS for w in words) and not any(w in _ENGLISH_WORDS for w in words)


def english_only(docs):
    """Drop books Hardcover says are not English (or, if it doesn't know, that look foreign by title)."""
    ids = [int(d["id"]) for d in docs if d.get("id") and int(d["id"]) not in _lang_cache]
    if ids:
        try:
            data = _gql(_LANG_QUERY, {"ids": ids})
            for b in data.get("books", []):
                langs = [((b.get(k) or {}).get("language") or {}).get("language") or "" for k in _EDITION_KEYS]
                langs = [l for l in langs if l]
                _lang_cache[int(b["id"])] = (True if any(l.startswith("English") for l in langs)
                                             else False if langs else None)
            for i in ids:
                _lang_cache.setdefault(i, None)
        except Exception as exc:
            logger.warning("Hardcover language lookup failed: %s", exc)
            return docs
    kept = []
    for d in docs:
        flag = _lang_cache.get(int(d["id"])) if d.get("id") else None
        title = d.get("title", "")
        words = re.findall(r"[a-zà-ÿ']+", title.lower())
        # Editions are sometimes mislabelled English, so two foreign function words and no English ones also counts
        strongly_foreign = (sum(w in _FOREIGN_WORDS for w in words) >= 2
                            and not any(w in _ENGLISH_WORDS for w in words))
        if flag is False or strongly_foreign or (flag is None and _looks_foreign(title)):
            continue
        kept.append(d)
    return kept


def search_books(query, per_page=20):
    """Search Hardcover for the UI search box. Returns a list of UI book dicts, English only."""
    if not enabled() or not query:
        return []
    try:
        docs = _search(query, per_page=per_page + 15, filter_by="compilation:=false", use_cache=False)
    except Exception as exc:
        logger.warning("Hardcover search failed for '%s': %s", query, exc)
        return []
    return [to_book(d) for d in collapse_variations(english_only(docs))][:per_page]


def collapse_variations(docs):
    """One result per title: Hardcover lists many entries for the same book (editions, reprints).

    Entries with the same normalised title and an author surname in common are one book. The most-read
    entry wins and keeps the position of the first one seen, so relevance order is preserved.
    """
    def surnames(d):
        return {re.findall(r"[a-z]+", n.lower())[-1] for n in (d.get("author_names") or []) if re.findall(r"[a-z]+", n.lower())}

    groups = []  # [normalised title, surnames, best doc]
    for d in docs:
        title, names = _norm(d.get("title", "")), surnames(d)
        for g in groups:
            if g[0] == title and (g[1] & names or not names or not g[1]):
                g[1] |= names
                if (d.get("users_count") or 0) > (g[2].get("users_count") or 0):
                    g[2] = d
                break
        else:
            groups.append([title, names, d])
    return [g[2] for g in groups]


def list_books(filter_by, sort="users_count:desc", per_page=20):
    """Return UI book dicts for a filtered/sorted Hardcover list (Discover rows), English only."""
    docs = _search("*", per_page=per_page + 10, filter_by=filter_by, sort=sort, use_cache=False)
    return [to_book(d) for d in collapse_variations(english_only(docs))][:per_page]


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
    return [to_book(d) for d in collapse_variations(english_only(docs))][:limit]


def get_book(book_id):
    """Return a UI book dict extended with description, rating, genres and series, or None."""
    if not enabled() or not book_id:
        return None
    try:
        docs = _search("*", per_page=1, filter_by=f"id:=[{int(book_id)}]")
    except Exception as exc:
        logger.warning("Hardcover details failed for %s: %s", book_id, exc)
        return None
    if not docs:
        return None
    d = docs[0]
    book = to_book(d)
    book.update({
        "description": re.sub(r"<[^>]+>", " ", d.get("description") or "").strip(),
        "subtitle": d.get("subtitle") or "",
        "rating": round(d.get("rating") or 0, 2),
        "ratings_count": d.get("ratings_count") or 0,
        "genres": (d.get("genres") or [])[:6],
        "series": (d.get("series_names") or [""])[0],
        "series_position": d.get("featured_series_position"),
        "release_date": d.get("release_date") or "",
        "has_audiobook": bool(d.get("has_audiobook")),
        "has_ebook": bool(d.get("has_ebook")),
        "slug": d.get("slug") or "",
    })
    return book
