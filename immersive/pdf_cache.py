"""Atomic per-page checkpoints for translated PDF pages in a local job directory."""

from __future__ import annotations

from contextlib import closing, contextmanager
from functools import lru_cache
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sqlite3

import pymupdf as pdf


@lru_cache(maxsize=1)
def _engine_key():
    stamp = hashlib.sha256(b'pdf-page-cache/1')
    for module in sorted(Path(__file__).parent.glob('pdf_*.py')):
        stamp.update(module.read_bytes())
    for package in ('pymupdf', 'pypdf', 'pypdfium2', 'fonttools'):
        try:
            stamp.update(version(package).encode())
        except PackageNotFoundError:
            pass
    directory = Path(os.environ.get('SystemRoot') or os.environ.get('WINDIR') or '.') / 'Fonts'
    for name in ('simsun.ttc', 'simhei.ttf', 'times.ttf', 'timesbd.ttf', 'timesi.ttf', 'timesbi.ttf'):
        path = directory / name
        if path.is_file():
            info = path.stat()
            stamp.update(f'{name}:{info.st_size}:{info.st_mtime_ns}'.encode())
    return stamp.hexdigest()


class PageCache:
    def __init__(self, conn, source_hash, mode):
        self.conn = conn
        self.namespace = (source_hash, _engine_key(), mode)
        conn.execute('CREATE TABLE IF NOT EXISTS pages (number INTEGER PRIMARY KEY, '
                     'digest TEXT NOT NULL, warnings TEXT NOT NULL, snapshot BLOB, checksum BLOB NOT NULL)')

    def key(self, number, ids, translations, sources):
        parts = [(i, translations[i], None if sources is None else sources[i]) for i in sorted(ids)]
        data = json.dumps((self.namespace, number, sources is not None, parts), ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()

    def read(self, number, key):
        row = self.conn.execute('SELECT warnings, snapshot, checksum FROM pages WHERE number=? AND digest=?',
                                (number, key)).fetchone()
        if row is None:
            return None
        try:
            if hashlib.sha256(row[1] or b'').digest() != row[2]:
                return None
            warnings = json.loads(row[0])
            if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
                return None
            if row[1] is None:
                return warnings, None  # An unchanged page uses the baked original for both halves.
            page = pdf.open(stream=row[1], filetype='pdf')
            if len(page) == 1 and not page.is_repaired:
                return warnings, page
            page.close()
        except (ValueError, RuntimeError):
            pass  # A damaged snapshot can be rebuilt from the source and its checked translations.
        return None

    def save(self, number, key, warnings, snapshot):
        data = snapshot.tobytes(garbage=4, deflate=True, use_objstms=1) if snapshot is not None else None
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?)',
                              (number, key, json.dumps(warnings, ensure_ascii=False), data,
                               hashlib.sha256(data or b'').digest()))


@contextmanager
def page_cache(directory, source, source_hash, mode):
    if directory is None:
        yield None
        return
    if source_hash is None:
        with open(source, 'rb') as stream:
            source_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / 'pages.sqlite3', timeout=30)) as conn:
        yield PageCache(conn, source_hash, mode)
