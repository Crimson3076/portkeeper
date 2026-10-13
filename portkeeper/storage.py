import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier():
    return uuid4().hex


class Store:
    """Small serialized store; each operation uses an independent connection."""

    tables = {"projects", "captures", "jobs", "artifacts", "notifications", "preservations", "settings", "proposals", "imports", "playtests", "trials"}

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            for table in self.tables:
                db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, table, record):
        assert table in self.tables
        with self.lock, self.connect() as db:
            db.execute(f"INSERT INTO {table} (id, body) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                       (record["id"], json.dumps(record)))
        return record

    def get(self, table, record_id):
        assert table in self.tables
        with self.lock, self.connect() as db:
            row = db.execute(f"SELECT body FROM {table} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown {table.rstrip('s')}")
        return json.loads(row[0])

    def all(self, table):
        assert table in self.tables
        with self.lock, self.connect() as db:
            rows = db.execute(f"SELECT body FROM {table} ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, table, record_id, **changes):
        with self.lock:
            record = self.get(table, record_id)
            record.update(changes)
            return self.put(table, record)

    def delete(self, table, record_id):
        assert table in self.tables
        with self.lock, self.connect() as db:
            db.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))

    def append_log(self, job_id, message):
        with self.lock:
            job = self.get("jobs", job_id)
            job.setdefault("logs", []).append(f"{now()}  {str(message)[:2000]}")
            job["logs"] = job["logs"][-500:]
            self.put("jobs", job)
