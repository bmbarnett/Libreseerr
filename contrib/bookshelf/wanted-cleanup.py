#!/usr/bin/env python3
"""Unmonitor non-English books that are still wanted (monitored, no file) in the Bookshelf instances.

Nothing is deleted. Books with files are never touched.
Usage: wanted-cleanup.py [--dry-run] [--instance ebook|audio]
"""
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bookshelf_lang

INSTANCES = {"ebook": ("bookshelf", 8787), "audio": ("bookshelf-audio", 8789)}
dry = "--dry-run" in sys.argv
only = sys.argv[sys.argv.index("--instance") + 1] if "--instance" in sys.argv else None

lock = open("/tmp/wanted-cleanup.lock", "w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit("already running")


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def api_factory(container, port):
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


for name, (container, port) in INSTANCES.items():
    if only and name != only:
        continue
    api = api_factory(container, port)
    wanted = [b for b in api("GET", "/book")
              if b.get("monitored") and not (b.get("statistics") or {}).get("bookFileCount")]
    log(f"{name}: {len(wanted)} wanted books")
    foreign = bookshelf_lang.foreign_ids(wanted)
    titles = {b["id"]: b["title"] for b in wanted}
    for bid, reason in list(foreign.items())[:(300 if "--verbose" in sys.argv else 15)]:
        log(f"  foreign ({reason}): {titles[bid][:70]}")
    log(f"{name}: {len(foreign)} foreign wanted books" + (" (dry run)" if dry else ", unmonitoring"))
    ids = list(foreign)
    if ids and not dry:
        for i in range(0, len(ids), 200):
            api("PUT", "/book/monitor", {"bookIds": ids[i:i + 200], "monitored": False})
