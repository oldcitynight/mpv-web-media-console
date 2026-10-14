"""Small page windows over disk-backed, immutable directory snapshots.

Only names/basic stat facts are indexed. Preview lookup and metadata probes run
for returned windows only. Eviction/changes fail explicitly, never truncate.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path


class DirectoryChanged(ValueError):
    pass


_lock = threading.RLock()
_indexes = OrderedDict()


def window(key, signature, entries, *, offset, limit, version, sort):
    with _lock:
        cache_key = (key, signature)
        cached = _indexes.get(cache_key)
        if cached and time.monotonic() - cached[1] > 120:
            cached[0].cleanup(); del _indexes[cache_key]; cached = None
        if cached is None:
            temporary = tempfile.TemporaryDirectory(prefix="mpv-directory-index-")
            path = Path(temporary.name) / "entries.sqlite"
            digest = hashlib.sha256()
            digest.update(repr(signature).encode())
            try:
                with sqlite3.connect(path) as db:
                    db.execute("CREATE TABLE entries(path TEXT PRIMARY KEY,name TEXT,folded TEXT,directory INTEGER,size INTEGER,modified TEXT,kind TEXT)")
                    for entry in entries():
                        row = (entry.path, entry.name, entry.name.casefold(), int(entry.is_directory), entry.size_bytes, entry.modified_at, entry.media_kind)
                        db.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET name=excluded.name,folded=excluded.folded WHERE (excluded.folded,excluded.name)<(entries.folded,entries.name)",row)
                    # Deterministic fingerprint, independent of filesystem enumeration order.
                    for row in db.execute("SELECT * FROM entries ORDER BY path"):
                        digest.update(json.dumps(row,ensure_ascii=False).encode())
                    db.execute("CREATE INDEX names ON entries(directory DESC,folded,name,path)")
                    total = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                    files = db.execute("SELECT COUNT(*) FROM entries WHERE directory=0").fetchone()[0]
                cached = (temporary, time.monotonic(), path, digest.hexdigest(), total, files)
                _indexes[cache_key] = cached
            except BaseException:
                temporary.cleanup(); raise
            while len(_indexes) > 4:
                _indexes.popitem(last=False)[1][0].cleanup()
        _indexes.move_to_end(cache_key)
        if version is not None and version != cached[3]:
            raise DirectoryChanged("Directory changed; reload from the first page.")
        order = {"name": "directory DESC,folded,name,path", "name_desc": "directory DESC,folded DESC,name DESC,path DESC", "modified": "directory DESC,modified DESC,folded,name,path"}[sort]
        with sqlite3.connect(cached[2]) as db:
            rows = db.execute(f"SELECT name,path,directory,size,modified,kind FROM entries ORDER BY {order} LIMIT ? OFFSET ?",(limit,offset)).fetchall()
        return rows,cached[4],cached[5],cached[3]
