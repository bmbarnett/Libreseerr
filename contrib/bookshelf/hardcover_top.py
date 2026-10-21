"""Hardcover's most-read books for an author (English, non-compilation), used by curated-books.py."""
import json
import re
import time
import urllib.error
import urllib.request

import bookshelf_lang

SEARCH_QUERY = ('query($q:String!,$f:String){ search(query:$q, query_type:"Book", per_page:40, page:1, '
                'filter_by:$f, sort:"users_count:desc"){ results } }')


def _gql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    for attempt in range(5):
        bookshelf_lang.throttle()
        req = urllib.request.Request(bookshelf_lang.API_URL, data=body, headers={
            "Authorization": bookshelf_lang._auth(), "Content-Type": "application/json",
            "User-Agent": "homelab-bookshelf"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                bookshelf_lang.note_rate_limit(e)
                if time.time() < bookshelf_lang._blocked_until:
                    raise bookshelf_lang.QuotaExhausted("Hardcover daily quota used up")
                if attempt < 4:
                    time.sleep(15 * (attempt + 1))      # per-minute limit: back off 15, 30, 45, 60 s
                    continue
            raise


def author_top_books(author_name):
    """Return [{"id", "title", "users"}] sorted most-read first; matching is surname plus first initial."""
    parts = re.findall(r"[a-z]+", author_name.lower())
    if not parts:
        return []
    last, first = parts[-1], parts[0][0]

    def by_author(doc):
        for name in doc.get("author_names") or []:
            p = re.findall(r"[a-z]+", name.lower())
            if p and p[-1] == last and p[0][0] == first:
                return True
        return False

    data = _gql(SEARCH_QUERY, {"q": author_name, "f": "compilation:=false"})
    docs = [h["document"] for h in data["search"]["results"]["hits"] if by_author(h["document"])]
    # Reuse the language check, which expects Bookshelf-shaped dicts
    pseudo = [{"id": int(d["id"]), "foreignBookId": d["id"], "title": d.get("title", "")} for d in docs]
    foreign = bookshelf_lang.foreign_ids(pseudo) if pseudo else {}
    return [{"id": int(d["id"]), "title": d.get("title", ""), "users": d.get("users_count") or 0}
            for d in docs if int(d["id"]) not in foreign]
