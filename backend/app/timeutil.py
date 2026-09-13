"""Shared time helpers (kept dependency-free to avoid import cycles)."""
from __future__ import annotations

from datetime import datetime, timezone


def utc(dt) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else utc(dt).isoformat()
