"""Lenient column types and parsers for data written by EXTERNAL processes.

An external agent may write the SQLite file directly (outside this app). One malformed
value (``'MX, LATAM'`` in a JSON column, ``'Oct 29 2026'`` in a date column, ``'50k'`` in a
number column) must never crash the whole UI. These types keep the normal storage format
(the same DDL and bind processing as the standard SQLAlchemy types) but read leniently:
values that cannot be interpreted come back as ``None`` / raw text, and
``SyncService.repair_jobs`` later rewrites them in canonical form.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.types import TypeDecorator

_DT_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d", "%Y.%m.%d",
               "%b %d %Y", "%b %d, %Y", "%B %d %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")
_NUM_CLEAN = re.compile(r"[,\s$€£]")


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort datetime (naive UTC). None when the value cannot be interpreted unambiguously."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        try:
            return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
        except OverflowError:
            return None
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:  # epoch milliseconds
            value = value / 1000
        if 946_684_800 <= value <= 4_102_444_800:  # 2000-01-01 .. 2100-01-01
            return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, OverflowError):  # OverflowError: e.g. 9999-12-31T23:00-05:00
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    dt = parse_datetime(value)
    return dt.date() if dt else None


def parse_float(value: Any) -> float | None:
    """Finite float or None (NaN/inf are rejected)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    text = _NUM_CLEAN.sub("", str(value)).lower()
    mult = 1.0
    if text.endswith("k"):
        text, mult = text[:-1], 1000.0
    try:
        f = float(text) * mult
    except (ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def parse_int(value: Any) -> int | None:
    f = parse_float(value)
    return int(f) if f is not None else None


def parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "si", "sí", "y", "t"):
        return True
    if text in ("0", "false", "no", "n", "f", ""):
        return False
    return None


def parse_json(value: Any) -> Any:
    """JSON text → Python. Non-JSON text is returned unchanged (callers normalise its shape)."""
    if not isinstance(value, (str, bytes)):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _proc(fn, default=None):
    def process(value):
        if value is None:
            return default
        try:
            out = fn(value)
        except Exception:  # never let one external value break a whole query
            return default
        return default if out is None else out
    return process


class _Lenient(TypeDecorator):
    """Standard DDL + bind processing of ``impl``; lenient result processing."""
    cache_ok = True
    _reader = staticmethod(lambda v: v)

    def result_processor(self, dialect, coltype):  # noqa: D401 - SQLAlchemy hook
        return _proc(self._reader)


class LenientJSON(_Lenient):
    cache_ok = True
    impl = JSON
    _reader = staticmethod(parse_json)


class LenientDateTime(_Lenient):
    cache_ok = True
    impl = DateTime
    _reader = staticmethod(parse_datetime)


class LenientDate(_Lenient):
    cache_ok = True
    impl = Date
    _reader = staticmethod(parse_date)


class LenientFloat(_Lenient):
    cache_ok = True
    impl = Float
    _reader = staticmethod(parse_float)


class LenientInteger(_Lenient):
    cache_ok = True
    impl = Integer
    _reader = staticmethod(parse_int)


class LenientBoolean(_Lenient):
    cache_ok = True
    impl = Boolean
    _reader = staticmethod(parse_bool)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("create_constraint", False)
        super().__init__(*args, **kwargs)


def _as_text(v: Any) -> str:
    return v if isinstance(v, str) else (v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v))


class LenientString(_Lenient):
    cache_ok = True
    impl = String
    _reader = staticmethod(_as_text)


class LenientText(_Lenient):
    cache_ok = True
    impl = Text
    _reader = staticmethod(_as_text)
