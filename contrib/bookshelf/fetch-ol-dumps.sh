#!/bin/sh
# Download the Open Library monthly data dumps (works, authors, editions, ratings, reading log, Wikidata
# links) into their own folder, smallest first. Resumable: rerun it and it continues where it stopped.
# Usage: fetch-ol-dumps.sh [target-dir]    (default /opt/docker/catalogue/dumps)
DIR="${1:-/opt/docker/catalogue/dumps}"
mkdir -p "$DIR" || exit 1
cd "$DIR" || exit 1
for name in authors ratings reading-log wikidata works editions; do
    out="ol_dump_${name}.txt.gz"
    if [ -f "$out" ] && gzip -t "$out" 2>/dev/null; then
        echo "$(date '+%F %T') $name already complete"
        continue
    fi
    echo "$(date '+%F %T') downloading $name"
    if curl -fL -C - --retry 5 --retry-delay 30 --retry-connrefused -o "$out.part" \
            "https://openlibrary.org/data/ol_dump_${name}_latest.txt.gz"; then
        if gzip -t "$out.part"; then
            mv "$out.part" "$out"
            echo "$(date '+%F %T') $name ok: $(ls -l "$out" | awk '{print $5}') bytes"
        else
            echo "$(date '+%F %T') $name FAILED integrity check, removing"
            rm -f "$out.part"
        fi
    else
        echo "$(date '+%F %T') $name download failed (rerun to resume)"
    fi
done
echo "$(date '+%F %T') finished"
