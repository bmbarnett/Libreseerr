#!/usr/bin/env python3
"""Make every author's wanted list the books people actually read.

For every author it builds a canonical book list from Hardcover (readers, series, language) cross-checked
against Open Library (real works, one per title; see catalogue.py), and then
  * monitors those that are already in Bookshelf,
  * adds those that Bookshelf's catalogue is missing (classics are often missing under the real title),
  * unmonitors every other wanted book (monitored, no file). Books with files are never touched.
Newly monitored books get a search. Usage:
  curated-books.py [--dry-run] [--instance ebook|audio] [--author NAME] [--min-id N] [--max N]
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalogue

INSTANCES = {"ebook": ("bookshelf", 8787), "audio": ("bookshelf-audio", 8789)}
MIN_READERS = 40          # a book needs at least this many Hardcover readers...
MIN_SHARE = 0.04          # ...and this share of the author's most-read book
MAX_PER_AUTHOR = 25
JUNK = re.compile(
    r"\s/\s|\bvol(ume)?\b|\bcollection\b|\bcomplete\b|\bomnibus\b|\billustrated\b|\bannotated\b|\babridged\b|"
    r"\bedition\b|\bbox(ed)? set\b|\bbooks? [0-9]|\bstudy\b|\bguide\b|\bletters?\b|\bjuvenilia\b|\bsneak peek\b|"
    r"\binterview\b|\bpostcards?\b|\bjigsaw\b|\bpack\b|\bworks of\b|\bnovels\b|\bstories\b|\bwit and wisdom\b|"
    r"\bdiet\b|\bprayers\b|\band other\b|, and ", re.I)

argv = sys.argv
dry = "--dry-run" in argv
only_instance = argv[argv.index("--instance") + 1] if "--instance" in argv else None
only_author = argv[argv.index("--author") + 1].lower() if "--author" in argv else None
min_id = int(argv[argv.index("--min-id") + 1]) if "--min-id" in argv else 1
max_authors = int(argv[argv.index("--max") + 1]) if "--max" in argv else 999

lock = open("/tmp/curated-books.lock", "w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit("already running")


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def norm(t):
    t = re.sub(r"[(\[].*?[)\]]", " ", t.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def make_api(container, port):
    key = subprocess.check_output(
        ["docker", "exec", container, "sh", "-c", "grep -o '<ApiKey>[^<]*' /config/config.xml | cut -d'>' -f2"],
        text=True).strip()

    def api(method, path, body=None):
        req = urllib.request.Request(f"http://localhost:{port}/api/v1{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Api-Key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    return api


REPORT = {}   # instance -> author -> keep/review lists, written to catalogue-report.json


def curate(api, author, profile_id, root, name):
    chosen = catalogue.select_books(author["authorName"])
    if not chosen or not chosen["keep"]:
        log(f"  {author['authorName']}: no usable Hardcover data, skipped")
        return
    keep, review = chosen["keep"], chosen["review"]
    keep_ids = {b["id"] for b in keep}

    books = api("GET", f"/book?authorId={author['id']}")
    by_hc = {int(b["foreignBookId"]): b for b in books}
    # A book you already have (a file under any Hardcover id) must not be added again under its canonical id
    have = {catalogue.short(b["title"]) for b in books if (b.get("statistics") or {}).get("bookFileCount", 0)}
    to_monitor = [by_hc[i]["id"] for i in keep_ids if i in by_hc and not by_hc[i].get("monitored")
                  and catalogue.short(by_hc[i]["title"]) not in have]
    missing = [b for b in keep if b["id"] not in by_hc and catalogue.short(b["title"]) not in have]
    off = [b["id"] for b in books if int(b["foreignBookId"]) not in keep_ids
           and b.get("monitored") and not (b.get("statistics") or {}).get("bookFileCount", 0)]
    log(f"  {author['authorName']}: keep {len(keep)} (monitor {len(to_monitor)}, add {len(missing)}), "
        f"unmonitor {len(off)}, review {len(review)}; keep: " + ", ".join(b["title"][:22] for b in keep[:6]))
    REPORT.setdefault(name, {})[author["authorName"]] = {
        "keep": [{k: b[k] for k in ("id", "title", "users", "source")} for b in keep],
        "review": [{k: b[k] for k in ("id", "title", "users", "reason")} for b in review]}
    if dry:
        return
    search_ids = list(to_monitor)
    if to_monitor:
        api("PUT", "/book/monitor", {"bookIds": to_monitor, "monitored": True})
    for b in missing:
        try:
            found = api("GET", f"/book/lookup?term=work:{b['id']}")
            if not found:
                continue
            f = found[0]
            edition = {"foreignEditionId": f.get("foreignEditionId", ""), "title": b["title"], "monitored": True}
            for k in ("images", "links", "ratings", "disambiguation", "remoteCover", "grabbed", "titleSlug"):
                if k in f:
                    edition[k] = f[k]
            added = api("POST", "/book", {
                "foreignBookId": f["foreignBookId"], "foreignEditionId": f.get("foreignEditionId", ""),
                "title": b["title"],          # Hardcover's title: Bookshelf's lookup may return a foreign edition title
                "authorId": author["id"], "qualityProfileId": profile_id, "rootFolderPath": root,
                "monitored": True, "anyEditionOk": True, "editions": [edition], "author": author,
                "addOptions": {"addType": "manual", "searchForMissingBooks": False}})
            if not added.get("monitored"):
                api("PUT", "/book/monitor", {"bookIds": [added["id"]], "monitored": True})
            search_ids.append(added["id"])
            log(f"    added {b['title'][:50]} ({b['users']} readers)")
        except Exception as exc:
            log(f"    could not add {b['title'][:50]}: {exc}")
        time.sleep(2)
    for i in range(0, len(off), 200):
        api("PUT", "/book/monitor", {"bookIds": off[i:i + 200], "monitored": False})
    if search_ids:
        api("POST", "/command", {"name": "BookSearch", "bookIds": search_ids})


for name, (container, port) in INSTANCES.items():
    if only_instance and name != only_instance:
        continue
    api = make_api(container, port)
    profile = api("GET", "/qualityprofile")[0]["id"]
    root = api("GET", "/rootfolder")[0]["path"]
    authors = [a for a in api("GET", "/author") if a["id"] >= min_id]
    if only_author:
        authors = [a for a in authors if only_author in a["authorName"].lower()]
    log(f"{name}: {len(authors)} author(s)")
    for a in authors[:max_authors]:
        try:
            curate(api, a, profile, root, name)
        except Exception as exc:
            log(f"  {a['authorName']} failed: {exc}")
        time.sleep(3)

try:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "catalogue-report-dryrun.json" if dry else "catalogue-report.json")
    with open(path, "w") as f:
        json.dump({"generated": time.strftime("%F %T"), "report": REPORT}, f, indent=1)
    log(f"report written to {path}")
except OSError as exc:
    log(f"could not write report: {exc}")
