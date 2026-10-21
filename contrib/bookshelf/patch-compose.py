#!/usr/bin/env python3
"""One-off: add the metadata-filter service to books01's docker-compose.yml and point both Bookshelf
instances at it. Idempotent; the original is saved next to it as docker-compose.yml.bak-metadata-filter."""
import shutil

path = "/opt/docker/books01/docker-compose.yml"
text = open(path).read()
if "metadata-filter:" in text:
    raise SystemExit("already patched")
shutil.copy(path, path + ".bak-metadata-filter")

old_url = '      METADATA_URL: "http://rreading-glasses:8788"'
assert text.count(old_url) == 2, "expected two Bookshelf services"
text = text.replace(old_url, '      METADATA_URL: "http://metadata-filter:8788"')
old_dep = "    depends_on:\n      - rreading-glasses\n"
assert text.count(old_dep) == 2, "expected two depends_on blocks"
text = text.replace(old_dep, "    depends_on:\n      - metadata-filter\n")

service = '''
  metadata-filter:
    image: python:3.12-alpine
    container_name: metadata-filter
    restart: unless-stopped
    depends_on:
      - rreading-glasses
    working_dir: /app
    command: ["python", "/app/proxy.py"]
    env_file: metadata-filter.env
    environment:
      PORT: "8788"
      CACHE_HOURS: "6"
    volumes:
      - /opt/docker/books01/metadata-filter.py:/app/proxy.py:ro
      - /opt/docker/books01/catalogue.py:/app/catalogue.py:ro
      - /opt/docker/books01/bookshelf_lang.py:/app/bookshelf_lang.py:ro
      - /opt/docker/books01/hardcover_top.py:/app/hardcover_top.py:ro
'''
marker = "\nvolumes:\n"
assert marker in text, "top-level volumes block not found"
text = text.replace(marker, service + marker, 1)
open(path, "w").write(text)
print("patched")
