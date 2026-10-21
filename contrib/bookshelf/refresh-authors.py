#!/usr/bin/env python3
"""Refresh every author in a Bookshelf instance, one at a time, so the metadata-filter proxy's curated
book lists replace the full catalogues. Books without files that are no longer in an author's list are
removed by Bookshelf; books with files are always kept by the proxy.
Usage: refresh-authors.py ebook|audio [--dry-run]
"""
import json
import subprocess
import sys
import time
import urllib.request

INSTANCES = {"ebook": ("bookshelf", 8787), "audio": ("bookshelf-audio", 8789)}
which = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in INSTANCES else None
if not which:
    sys.exit(__doc__)
container, port = INSTANCES[which]
dry = "--dry-run" in sys.argv
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


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def count(author_id):
    books = api("GET", f"/book?authorId={author_id}")
    return len(books), sum(1 for b in books if b.get("monitored") and not (b.get("statistics") or {}).get("bookFileCount"))


authors = api("GET", "/author")
log(f"{which}: refreshing {len(authors)} authors" + (" (dry run)" if dry else ""))
for a in authors:
    before = count(a["id"])
    if dry:
        log(f"  {a['authorName']}: {before[0]} books, {before[1]} wanted")
        continue
    cmd = api("POST", "/command", {"name": "RefreshAuthor", "authorId": a["id"]})
    t0 = time.time()
    status = "?"
    while time.time() - t0 < 900:
        c = api("GET", f"/command/{cmd['id']}")
        status = c["status"]
        if status in ("completed", "failed", "aborted", "cancelled"):
            break
        time.sleep(10)
    after = count(a["id"])
    log(f"  {a['authorName']}: {status}; books {before[0]} -> {after[0]}, wanted {before[1]} -> {after[1]}")
    time.sleep(5)
log("done")
