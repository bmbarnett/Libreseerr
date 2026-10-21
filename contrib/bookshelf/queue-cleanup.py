#!/usr/bin/env python3
"""Clear Bookshelf import failures that will never succeed (duplicates / wrong books):
delete from the queue + blocklist the release, leaving the files in the download client.
Cron: every 6 h. Usage: queue-cleanup.py [--dry-run]"""
import json, subprocess, sys, time, urllib.request

BASE = "http://localhost:8787/api/v1"
PATTERNS = ("same filesize", "not close enough", "couldn't find similar book", "not an upgrade")
dry = "--dry-run" in sys.argv
KEY = subprocess.check_output(["docker", "exec", "bookshelf", "sh", "-c",
                               "grep -o '<ApiKey>[^<]*' /config/config.xml | cut -d'>' -f2"], text=True).strip()


def api(method, path):
    req = urllib.request.Request(BASE + path, method=method, headers={"X-Api-Key": KEY})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


n = 0
for x in api("GET", "/queue?pageSize=500")["records"]:
    if x.get("trackedDownloadState") != "importFailed":
        continue
    msgs = " ".join(m.lower() for s in x.get("statusMessages", []) for m in s.get("messages", []))
    if not any(p in msgs for p in PATTERNS):
        continue
    print(time.strftime("%F %T"), "clear", x["title"][:70], "|", msgs[:60], flush=True)
    if not dry:
        api("DELETE", f"/queue/{x['id']}?removeFromClient=false&blocklist=true&skipRedownload=true")
    n += 1
print(time.strftime("%F %T"), "cleared", n, flush=True)
