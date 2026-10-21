"""Decide which books by an author are real, readable works, using Hardcover and Open Library together.

Hardcover knows how many people read a book, its series and which edition language it has, but it
lists hundreds of variations (foreign editions, study guides, box sets, one entry per edition).
Open Library groups editions into works and says how many editions a work has. A book is kept when
Hardcover says people read it and Open Library (or a series position) confirms it is a real work.
Two editions of the same title collapse into the most-read one.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import bookshelf_lang
import hardcover_top

HC_QUERY = ('query($q:String!,$f:String){ search(query:$q, query_type:"Book", per_page:80, page:1, '
            'filter_by:$f, sort:"users_count:desc"){ results } }')
MIN_READERS = 40            # a kept book needs this many Hardcover readers...
MIN_SHARE = 0.04            # ...and this share of the author's most-read book
SERIES_MIN_READERS = 20     # series books can be a little less popular
POPULAR_SHARE = 0.15        # this popular, Hardcover alone is enough
MAX_KEEP = 30

JUNK = re.compile(
    r"\s/\s|\bvol(ume)?\b|\bcollection\b|\bcomplete\b|\bomnibus\b|\billustrated\b|\bannotated\b|\babridged\b|"
    r"\bedition\b|\bbox(ed)? set\b|\bbooks? [0-9]|\bstudy\b|\bguide\b|\bletters?\b|\bjuvenilia\b|\bsneak peek\b|"
    r"\binterview\b|\bpostcards?\b|\bjigsaw\b|\bpack\b|\bworks of\b|\bnovels\b|\bstories\b|\bwit and wisdom\b|"
    r"\bdiet\b|\bprayers\b|\band other\b|, and |#\s?[0-9]|\bissue\b", re.I)


def norm(title):
    t = re.sub(r"[(\[].*?[)\]]", " ", (title or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"(sorcerer|philosopher) ?s stone", "philosophers stone", t)
    return re.sub(r"^(the|a|an) ", "", t)


def short(title):
    """Title without its subtitle ("Dune: Messiah" -> "dune", "The Hobbit, or There and Back Again" -> "hobbit")."""
    t = re.split(r"[:;]| - |, or ", title or "")[0]
    return norm(t)


def _name_match(author_name):
    parts = re.findall(r"[a-z]+", author_name.lower())
    last, first = (parts[-1], parts[0][0]) if parts else ("", "")

    def match(names):
        for n in names or []:
            p = re.findall(r"[a-z]+", n.lower())
            if p and p[-1] == last and p[0][0] == first:
                return True
        return False
    return match


def hardcover_works(author_name):
    data = hardcover_top._gql(HC_QUERY, {"q": author_name, "f": "compilation:=false"})
    match = _name_match(author_name)
    docs = [h["document"] for h in data["search"]["results"]["hits"] if match(h["document"].get("author_names"))]
    pseudo = [{"id": int(d["id"]), "foreignBookId": d["id"], "title": d.get("title", "")} for d in docs]
    foreign = bookshelf_lang.foreign_ids(pseudo) if pseudo else {}
    out = []
    for d in docs:
        if int(d["id"]) in foreign:
            continue
        out.append({"id": int(d["id"]), "title": d.get("title", ""), "users": d.get("users_count") or 0,
                    "series": (d.get("series_names") or [""])[0],
                    "position": d.get("featured_series_position"), "year": d.get("release_year")})
    return out


def openlibrary_works(author_name):
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode({
        "author": author_name, "sort": "editions", "limit": 80, "lang": "eng",
        "fields": "title,edition_count,first_publish_year,author_name"})
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-catalogue"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                docs = json.load(r).get("docs", [])
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                return None          # Open Library unavailable: caller falls back to Hardcover-only rules
            time.sleep(5)
    match = _name_match(author_name)
    works = {}
    for d in docs:
        if not match(d.get("author_name")):
            continue
        for key in {norm(d["title"]), short(d["title"])}:
            if key and (key not in works or d.get("edition_count", 0) > works[key]["editions"]):
                works[key] = {"title": d["title"], "editions": d.get("edition_count") or 0,
                              "year": d.get("first_publish_year")}
    return works


def select_books(author_name):
    """Return {"keep": [...], "review": [...]}; each item has id, title, users, source, reason."""
    hc = hardcover_works(author_name)
    if not hc:
        return None
    ol = openlibrary_works(author_name)
    best = hc[0]["users"] if hc else 0
    keep, review, seen = [], [], set()
    for b in sorted(hc, key=lambda x: -x["users"]):
        key = short(b["title"]) if ol else norm(b["title"])
        if JUNK.search(b["title"]) or norm(b["title"]) in seen or key in seen:
            continue
        in_ol = ol is not None and (norm(b["title"]) in ol or short(b["title"]) in ol)
        in_series = b["position"] is not None
        verdict = None
        if b["users"] >= max(MIN_READERS, MIN_SHARE * best):
            if in_ol or in_series or b["users"] >= POPULAR_SHARE * best or ol is None:
                verdict = "both" if in_ol else ("series" if in_series else "popular")
            else:
                review.append({**b, "source": "hardcover", "reason": "not found on Open Library"})
        elif in_series and b["users"] >= SERIES_MIN_READERS:
            verdict = "series"
        if verdict and len(keep) < MAX_KEEP:
            seen.update({norm(b["title"]), key})
            keep.append({**b, "source": verdict})
    return {"keep": keep, "review": review}
