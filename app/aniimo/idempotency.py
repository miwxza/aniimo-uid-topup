"""Idempotency / dedupe state for order placement.

Design goals (from review):
* An idempotency key is bound to a **fingerprint** of the order
  (uid|server|package|quantity). Reusing a key with different data is a 409.
* A failure **after** place_order was dispatched leaves the key ``unknown`` —
  the outcome may have succeeded upstream — so resends are blocked until the
  status is checked. A replay of a completed order returns the cached result
  (and the cached character) **without** re-running any upstream call.
* Key reservation is **atomic** (``INSERT … ON CONFLICT DO NOTHING``) so two
  workers racing on the same key cannot both be admitted.
* State is **durable**: the ``sqlite`` backend survives restarts; ``unknown`` /
  ``in_flight`` entries are never TTL-expired (only ``done`` ages out).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from app.errors import (
    DuplicateOrderError,
    IdempotencyKeyConflictError,
    OrderResultUnknownError,
)

Status = Literal["in_flight", "unknown", "done"]


@dataclass
class OrderResult:
    order_id: str
    status: str  # created | pending | unknown
    currency: str
    amount: float
    pay_url: str | None
    is_mock: bool = False


@dataclass
class Entry:
    status: Status
    fingerprint: str
    result: OrderResult | None
    character: dict | None
    ts: float


class StateStore(Protocol):
    def reserve(self, key: str, entry: Entry) -> Entry | None:
        """Atomically insert ``entry`` if the key is free.

        Returns ``None`` when this caller won the slot, or the EXISTING entry
        when the key was already taken.
        """

    def get(self, key: str) -> Entry | None: ...
    def put(self, key: str, entry: Entry) -> None: ...
    def delete(self, key: str) -> None: ...
    def delete_in_flight(self, key: str) -> None: ...
    def gc_done(self, older_than: float) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self._d: dict[str, Entry] = {}
        self._lock = threading.Lock()

    def reserve(self, key: str, entry: Entry) -> Entry | None:
        with self._lock:
            existing = self._d.get(key)
            if existing is not None:
                return existing
            self._d[key] = entry
            return None

    def get(self, key: str) -> Entry | None:
        with self._lock:
            return self._d.get(key)

    def put(self, key: str, entry: Entry) -> None:
        with self._lock:
            self._d[key] = entry

    def delete(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)

    def delete_in_flight(self, key: str) -> None:
        with self._lock:
            e = self._d.get(key)
            if e is not None and e.status == "in_flight":
                self._d.pop(key, None)

    def gc_done(self, older_than: float) -> None:
        now = time.time()
        with self._lock:
            for k in [k for k, e in self._d.items() if e.status == "done" and now - e.ts > older_than]:
                self._d.pop(k, None)


class SqliteStateStore:
    """Durable, atomic state. Safe for multiple workers sharing one db file."""

    # Expected columns and their SQL types. New columns are added to existing
    # tables via ALTER (see _migrate) so an older database keeps its rows —
    # dropping the db would discard the unknown/in_flight dedupe history that
    # prevents duplicate orders.
    _COLUMNS = {
        "key": "TEXT PRIMARY KEY",
        "status": "TEXT",
        "fingerprint": "TEXT",
        "result": "TEXT",
        "character": "TEXT",
        "ts": "REAL",
    }

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS order_state ("
            "key TEXT PRIMARY KEY, status TEXT, fingerprint TEXT, "
            "result TEXT, character TEXT, ts REAL)"
        )
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older table up to the current schema without data loss.

        A pre-'character' database (created by an earlier version) is upgraded
        by adding the missing column via ALTER TABLE; every existing row —
        including unknown/in_flight dedupe entries — is preserved. Legacy rows
        get character = NULL, which the guard/route handle explicitly.
        """
        with self._lock:
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(order_state)")}
            for col, decl in self._COLUMNS.items():
                if col not in existing:
                    # PRIMARY KEY cannot be added via ALTER, but 'key' always
                    # exists (the table has a PK); only data columns are added.
                    col_type = decl.replace(" PRIMARY KEY", "")
                    self._conn.execute(f"ALTER TABLE order_state ADD COLUMN {col} {col_type}")

    def _row_to_entry(self, row) -> Entry:
        status, fp, result_json, char_json, ts = row
        result = OrderResult(**json.loads(result_json)) if result_json else None
        character = json.loads(char_json) if char_json else None
        return Entry(status=status, fingerprint=fp, result=result, character=character, ts=ts)

    def reserve(self, key: str, entry: Entry) -> Entry | None:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO order_state(key,status,fingerprint,result,character,ts) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
                (key, entry.status, entry.fingerprint, None, None, entry.ts),
            )
            if cur.rowcount == 1:
                return None  # we won the slot
            row = self._conn.execute(
                "SELECT status, fingerprint, result, character, ts FROM order_state WHERE key=?",
                (key,),
            ).fetchone()
            return self._row_to_entry(row) if row else None

    def get(self, key: str) -> Entry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, fingerprint, result, character, ts FROM order_state WHERE key=?",
                (key,),
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def put(self, key: str, entry: Entry) -> None:
        result_json = json.dumps(asdict(entry.result)) if entry.result else None
        char_json = json.dumps(entry.character) if entry.character else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO order_state(key,status,fingerprint,result,character,ts) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "status=excluded.status, fingerprint=excluded.fingerprint, "
                "result=excluded.result, character=excluded.character, ts=excluded.ts",
                (key, entry.status, entry.fingerprint, result_json, char_json, entry.ts),
            )

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM order_state WHERE key=?", (key,))

    def delete_in_flight(self, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM order_state WHERE key=? AND status='in_flight'", (key,)
            )

    def gc_done(self, older_than: float) -> None:
        cutoff = time.time() - older_than
        with self._lock:
            self._conn.execute("DELETE FROM order_state WHERE status='done' AND ts < ?", (cutoff,))


class OrderGuard:
    def __init__(self, store: StateStore, done_ttl: float = 86400.0):
        self._store = store
        self._done_ttl = done_ttl

    def begin(self, key: str, fingerprint: str) -> Entry | None:
        """Atomically admit an order. Returns the cached ``Entry`` for an exact
        replay (caller reuses its stored result + character), or ``None`` when
        this caller owns a fresh slot. Raises on conflict / in-flight / unknown.
        """
        self._store.gc_done(self._done_ttl)  # only completed entries age out
        reserved = Entry("in_flight", fingerprint, None, None, time.time())
        existing = self._store.reserve(key, reserved)
        if existing is None:
            return None  # we own the slot — proceed with a fresh order
        if existing.fingerprint != fingerprint:
            raise IdempotencyKeyConflictError(key)
        if existing.status == "done" and existing.result is not None:
            return existing  # replay — NO upstream call needed
        if existing.status == "unknown":
            raise OrderResultUnknownError(key)
        raise DuplicateOrderError(key)  # in_flight

    def complete(self, key: str, fingerprint: str, result: OrderResult, character: dict) -> None:
        self._store.put(key, Entry("done", fingerprint, result, character, time.time()))

    def mark_unknown(self, key: str, fingerprint: str) -> None:
        """Post-dispatch failure: keep the key so resends stay blocked."""
        self._store.put(key, Entry("unknown", fingerprint, None, None, time.time()))

    def abort(self, key: str) -> None:
        """Pre-dispatch failure: free the key, but ONLY if still in_flight —
        never removes a completed ('done') or 'unknown' entry."""
        self._store.delete_in_flight(key)

    def resolve(self, key: str, result: OrderResult | None, fingerprint: str = "",
                character: dict | None = None) -> None:
        """Clear an 'unknown' entry after the order status has been checked.

        ``result`` set → the order was confirmed created (cache it); ``None`` →
        confirmed NOT created, free the key so it can be retried.
        """
        if result is None:
            self._store.delete(key)
        else:
            self.complete(key, fingerprint, result, character or {})


def build_state_store(backend: str, db_path: str) -> StateStore:
    return SqliteStateStore(db_path) if backend == "sqlite" else MemoryStateStore()
