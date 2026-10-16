"""Single source of truth for UID / server validation.

Used by both ``/verify`` and ``/order`` so the two endpoints never disagree on
what a well-formed UID or server looks like.
"""
from __future__ import annotations

from app.errors import InvalidServerError, InvalidUidError


def normalize_uid(uid: str) -> str:
    v = (uid or "").strip()
    if not v.isascii() or not v.isdigit() or not (4 <= len(v) <= 20):
        raise InvalidUidError(uid)
    return v


def normalize_server(server: str) -> str:
    v = (server or "").strip()
    # Server ids on the global cluster are small positive integers.
    if not v.isascii() or not v.isdigit() or not (1 <= len(v) <= 6) or int(v) < 1:
        raise InvalidServerError(server)
    return v
