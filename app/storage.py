import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ACTIVE = ("starting", "active", "uncertain", "stopping")


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    start_time TEXT NOT NULL DEFAULT '11:00',
                    duration INTEGER NOT NULL DEFAULT 60,
                    days TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]'
                );
                INSERT OR IGNORE INTO schedule(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    occurrence TEXT UNIQUE,
                    source TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs((1))
                    WHERE status IN ('starting','active','uncertain','stopping');
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controller_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS saved_login (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    encrypted BLOB NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def schedule(self):
        with self.connection() as db:
            row = dict(db.execute("SELECT * FROM schedule WHERE id=1").fetchone())
        row["enabled"] = bool(row["enabled"])
        row["days"] = json.loads(row["days"])
        return row

    def bound_device(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM controller_meta WHERE key='device'").fetchone()
            return row[0] if row else None

    def bind_device(self, device_id):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO controller_meta(key,value) VALUES('device',?)",
                (device_id,),
            )

    def auto_login_paused(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM controller_meta WHERE key='auto_login_paused'"
            ).fetchone()
            return bool(row and row[0] == "1")

    def pause_auto_login(self, paused):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO controller_meta(key,value) VALUES('auto_login_paused',?)",
                ("1" if paused else "0",),
            )

    def encrypted_login(self):
        with self.connection() as db:
            row = db.execute("SELECT encrypted FROM saved_login WHERE id=1").fetchone()
            return bytes(row[0]) if row else None

    def save_encrypted_login(self, encrypted):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO saved_login(id,encrypted) VALUES(1,?)",
                (sqlite3.Binary(encrypted),),
            )

    def forget_login(self):
        with self.connection() as db:
            db.execute("DELETE FROM saved_login")
        with self.connection() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def save_schedule(self, enabled: bool, start: str, duration: int, days: list[int]):
        with self.connection() as db:
            db.execute(
                "UPDATE schedule SET enabled=?,start_time=?,duration=?,days=? WHERE id=1",
                (int(enabled), start, duration, json.dumps(sorted(set(days)))),
            )

    def active(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE status IN (?,?,?,?) ORDER BY id DESC LIMIT 1", ACTIVE
            ).fetchone()
            return dict(row) if row else None

    def reserve(self, source: str, start: datetime, end: datetime, occurrence: str | None):
        with self.connection() as db:
            try:
                result = db.execute(
                    """INSERT INTO runs(occurrence,source,starts_at,ends_at,status,message)
                       VALUES(?,?,?,?, 'starting', 'Sending boost command')""",
                    (occurrence, source, start.isoformat(), end.isoformat()),
                )
            except sqlite3.IntegrityError:
                return None
            return result.lastrowid

    def update_run(self, run_id: int, status: str, message: str):
        with self.connection() as db:
            db.execute("UPDATE runs SET status=?,message=? WHERE id=?", (status, message, run_id))

    def has_occurrence(self, occurrence: str):
        with self.connection() as db:
            return (
                db.execute("SELECT 1 FROM runs WHERE occurrence=?", (occurrence,)).fetchone()
                is not None
            )

    def event(self, now: datetime, message: str):
        with self.connection() as db:
            db.execute(
                "INSERT INTO events(created_at,message) VALUES(?,?)", (now.isoformat(), message)
            )
            db.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT 200)"
            )

    def history(self):
        with self.connection() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12")
            ]
