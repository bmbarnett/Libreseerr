#!/usr/bin/env python3
"""Nightly backfill for authors LibreSeerr added with the "None" metadata profile.

Switches them one at a time to the "Standard" (English) profile, refreshes, then monitors only the
best edition of each non-junk book (see tidy) and searches for them. Stops for the night on any
error / throttling.
Usage: author-backfill.py [--dry-run] [--max N] [--tidy-only | --prune-only]
"""
import json, os, re, subprocess, sys, time, urllib.request, fcntl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bookshelf_lang

BASE = "http://localhost:8787/api/v1"
MAX_PER_RUN = 4
PAUSE = 180
REFRESH_TIMEOUT = 45 * 60

dry = "--dry-run" in sys.argv
if "--max" in sys.argv:
    MAX_PER_RUN = int(sys.argv[sys.argv.index("--max") + 1])

lock = open("/tmp/author-backfill.lock", "w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit("already running")


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


KEY = subprocess.check_output(
    ["docker", "exec", "bookshelf", "sh", "-c", "grep -o '<ApiKey>[^<]*' /config/config.xml | cut -d'>' -f2"],
    text=True).strip()


def api(method, path, body=None, timeout=300):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Api-Key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def throttled():
    out = subprocess.run("docker logs --since 10m rreading-glasses 2>&1 | grep -c 'returned error 429'",
                         shell=True, capture_output=True, text=True).stdout.strip()
    return int(out or 0)


# Titles that are compilations, study aids, extras or non-English editions: never worth auto-searching.
JUNK = re.compile(
    r"\s/\s|\bvol(ume)?\b|\bcollection\b|\bcomplete\b|\bomnibus\b|\billustrated\b|\bannotated\b|"
    r"\babridged\b|\bedition\b|\bbox(ed)? set\b|\bbooks? [0-9]|\bseries\b|\bstudy\b|\bguide\b|"
    r"\bletters?\b|\bjuvenilia\b|\bsneak peek\b|\binterview\b|\bpostcards?\b|\bjigsaw\b|\bpack\b|"
    r"\bworks of\b|\bnovels\b|\bstories\b|\bwit and wisdom\b|\bdiet\b|\bprayers\b|\band other\b|, and |"
    r"[À-ÿ]", re.I)


def norm(t):
    t = re.sub(r"[(\[].*?[)\]]", " ", t.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def tidy(aid):
    """Monitor only the best edition of each non-junk book; unmonitor the rest.
    Never unmonitors a book that has a file. Returns (total, kept, newly_on, newly_off)."""
    books = api("GET", f"/book?authorId={aid}")
    best = {}
    for b in books:
        if JUNK.search(b["title"]):
            continue
        k = norm(b["title"])
        score = (b["statistics"]["bookFileCount"] > 0, (b.get("ratings") or {}).get("votes") or 0)
        if k not in best or score > best[k][0]:
            best[k] = (score, b["id"])
    keep = {v[1] for v in best.values()}
    want_on = [] if "--prune-only" in sys.argv else [b["id"] for b in books if b["id"] in keep and not b.get("monitored")]
    want_off = [b["id"] for b in books if b["id"] not in keep and b.get("monitored")
                and b["statistics"]["bookFileCount"] == 0]
    if not dry:
        if want_on:
            api("PUT", "/book/monitor", {"bookIds": want_on, "monitored": True})
        if want_off:
            api("PUT", "/book/monitor", {"bookIds": want_off, "monitored": False})
    return len(books), len(keep), len(want_on), len(want_off)


def drop_foreign(aid):
    """Unmonitor this author's wanted (monitored, no file) books that are not English."""
    wanted = [b for b in api("GET", f"/book?authorId={aid}")
              if b.get("monitored") and not b["statistics"]["bookFileCount"]]
    foreign = list(bookshelf_lang.foreign_ids(wanted))
    if foreign and not dry:
        api("PUT", "/book/monitor", {"bookIds": foreign, "monitored": False})
    return len(foreign)


profiles = {p["name"].lower(): p["id"] for p in api("GET", "/metadataprofile")}
none_id, std_id = profiles["none"], profiles["standard"]

if "--tidy-only" in sys.argv or "--prune-only" in sys.argv:
    # LibreSeerr-added authors only (ids 15+); the original authors are left alone.
    for a in api("GET", "/author"):
        if a["id"] >= 15 and a["metadataProfileId"] == std_id:
            log(a["authorName"], "(total, kept, +on, -off)", tidy(a["id"]), "foreign off:", drop_foreign(a["id"]))
    sys.exit(0)

todo = [a for a in api("GET", "/author") if a["metadataProfileId"] == none_id]
log(f"{len(todo)} author(s) waiting")
done = 0
for a in todo:
    if done >= MAX_PER_RUN:
        break
    n429 = throttled()
    if n429 > 25:
        log(f"metadata server throttled ({n429} 429s in 10 min), stopping for tonight")
        break
    aid, name = a["id"], a["authorName"]
    log(f"backfill {name} (id {aid})")
    if dry:
        done += 1
        continue
    full = api("GET", f"/author/{aid}")
    full.update(metadataProfileId=std_id, monitored=True, monitorNewItems="all")
    api("PUT", f"/author/{aid}?moveFiles=false", full)
    try:
        cmd = api("POST", "/command", {"name": "RefreshAuthor", "authorId": aid})
        t0 = time.time()
        while True:
            c = api("GET", f"/command/{cmd['id']}")
            if c["status"] == "completed":
                break
            if c["status"] in ("failed", "aborted", "cancelled") or time.time() - t0 > REFRESH_TIMEOUT:
                raise RuntimeError(f"refresh {c['status']}")
            time.sleep(15)
        total, kept, on, off = tidy(aid)
        foreign = drop_foreign(aid)
        api("POST", "/command", {"name": "AuthorSearch", "authorId": aid})
        log(f"  {name}: {total} books, {kept} kept monitored (+{on}/-{off}), {foreign} foreign unmonitored, search queued")
    except Exception as e:
        log(f"  {name} failed ({e}); reverting to None profile and stopping")
        full = api("GET", f"/author/{aid}")
        full["metadataProfileId"] = none_id
        api("PUT", f"/author/{aid}?moveFiles=false", full)
        break
    done += 1
    time.sleep(PAUSE)
log(f"done, {done} processed")
