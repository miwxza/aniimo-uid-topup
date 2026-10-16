"""Idempotency / dedupe state for order placement.

Design goals (from review):
* An idempotency key is bound to a **fingerprint** of the order
  (uid|server|package|quantity). Reusing a key with different data is a 409,
  never a silent replay onto a different character/amount.
* A failure that happens **after** place_order was dispatched (invalid response,
  5xx, timeout) leaves the key in an ``unknown`` state — the outcome may have
  succeeded upstream — so an identical resend is blocked until the order status
  is checked and the entry is explicitly resolved.
* State is **durable**: a ``sqlite`` backend survives process restarts; the
  in-memory backend is for tests/dev. Swap for Redis in a cluster — same
  contract. ``unknown`` / ``in_flight`` entries are **never** TTL-expired; only
  completed (``done``) entries age out.
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
    ts: float


class StateStore(Protocol):
    def get(self, key: str) -> Entry | None: ...
    def put(self, key: str, entry: Entry) -> None: ...
    def delete(self, key: str) -> None: ...
    def gc_done(self, older_than: float) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self._d: dict[str, Entry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Entry | None:
        with self._lock:
            return self._d.get(key)

    def put(self, key: str, entry: Entry) -> None:
        with self._lock:
            self._d[key] = entry

    def delete(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)

    def gc_done(self, older_than: float) -> None:
        now = time.time()
        with self._lock:
            for k in [k for k, e in self._d.items() if e.status == "done" and now - e.ts > older_than]:
                self._d.pop(k, None)


class SqliteStateStore:
    """Durable state; survives restarts. Single-writer-safe via a lock."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS order_state ("
            "key TEXT PRIMARY KEY, status TEXT, fingerprint TEXT, result TEXT, ts REAL)"
        )
        self._conn.commit()

    def get(self, key: str) -> Entry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, fingerprint, result, ts FROM order_state WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        status, fp, result_json, ts = row
        result = OrderResult(**json.loads(result_json)) if result_json else None
        return Entry(status=status, fingerprint=fp, result=result, ts=ts)

    def put(self, key: str, entry: Entry) -> None:
        result_json = json.dumps(asdict(entry.result)) if entry.result else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO order_state(key,status,fingerprint,result,ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET status=excluded.status, "
                "fingerprint=excluded.fingerprint, result=excluded.result, ts=excluded.ts",
                (key, entry.status, entry.fingerprint, result_json, entry.ts),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM order_state WHERE key=?", (key,))
            self._conn.commit()

    def gc_done(self, older_than: float) -> None:
        cutoff = time.time() - older_than
        with self._lock:
            self._conn.execute(
                "DELETE FROM order_state WHERE status='done' AND ts < ?", (cutoff,)
            )
            self._conn.commit()


class OrderGuard:
    def __init__(self, store: StateStore, done_ttl: float = 86400.0):
        self._store = store
        self._done_ttl = done_ttl

    def begin(self, key: str, fingerprint: str) -> OrderResult | None:
        """Admit an order. Returns a cached result for an exact replay, or raises.

        * different data for the same key → ``IDEMPOTENCY_KEY_CONFLICT``
        * already in flight → ``DUPLICATE_ORDER``
        * previously left ``unknown`` → ``ORDER_RESULT_UNKNOWN`` (check status first)
        """
        self._store.gc_done(self._done_ttl)  # only completed entries age out
        e = self._store.get(key)
        if e is not None:
            if e.fingerprint != fingerprint:
                raise IdempotencyKeyConflictError(key)
            if e.status == "done" and e.result is not None:
                return e.result
            if e.status == "unknown":
                raise OrderResultUnknownError(key)
            raise DuplicateOrderError(key)  # in_flight
        self._store.put(key, Entry("in_flight", fingerprint, None, time.time()))
        return None

    def complete(self, key: str, fingerprint: str, result: OrderResult) -> None:
        self._store.put(key, Entry("done", fingerprint, result, time.time()))

    def mark_unknown(self, key: str, fingerprint: str) -> None:
        """Post-dispatch failure: keep the key so resends are blocked."""
        self._store.put(key, Entry("unknown", fingerprint, None, time.time()))

    def abort(self, key: str) -> None:
        """Pre-dispatch failure: the order was never sent, so the key is freed."""
        self._store.delete(key)

    def resolve(self, key: str, result: OrderResult | None, fingerprint: str = "") -> None:
        """Clear an ``unknown`` entry after the order status has been checked."""
        if result is None:
            self._store.delete(key)
        else:
            self.complete(key, fingerprint, result)


def build_state_store(backend: str, db_path: str) -> StateStore:
    return SqliteStateStore(db_path) if backend == "sqlite" else MemoryStateStore()
