"""Normalisation helpers shared by adapters and services.

Adapters must hand back data in the documented normalised shapes (see
docs/DATA_SOURCES.md). These helpers make that easy and enforce privacy rules
(e.g. passenger names are dropped).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

IST = ZoneInfo("Asia/Kolkata")

WL_CODES = {"WL", "GNWL", "RLWL", "PQWL", "RSWL", "TQWL", "CKWL", "RQWL", "NPWL", "DPWL", "LDWL", "HOWL", "SSWL"}
KNOWN_CODES = WL_CODES | {"CNF", "RAC", "CAN", "NOSB", "NR", "RLGN", "REGRET", "PQRAC", "RELEASED"}
_ALIASES = {"CONFIRMED": "CNF", "CONFIRM": "CNF", "CANCELLED": "CAN", "CANCELED": "CAN", "CNCL": "CAN"}

# Coach codes such as S5, B2, A1, H1, M3, C4, D1, E1, 2S... (letter(s) followed by digits)
_COACH_RE = re.compile(r"^[A-Z]{1,3}\d{1,2}$")


def status_code(raw: str | None) -> str | None:
    """Map a raw booking/current status string to a short code (CNF, RAC, WL, ...)."""
    if not raw:
        return None
    token = re.split(r"[\s/,\-]+", raw.strip().upper())[0]
    token = _ALIASES.get(token, token)
    if token in KNOWN_CODES:
        return token
    # bare coach/berth means confirmed, e.g. "S5/32/LB"
    if _COACH_RE.match(token):
        return "CNF"
    return "OTHER"


def is_waitlisted(code: str | None) -> bool:
    return bool(code) and code in WL_CODES


def split_coach_berth(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    parts = [p for p in re.split(r"[\s/,]+", raw.strip().upper()) if p]
    if parts and parts[0] in ("CNF", "RAC"):
        parts = parts[1:]
    if len(parts) >= 2 and _COACH_RE.match(parts[0]) and parts[1].isdigit():
        return parts[0], parts[1]
    return None, None


def chart_status(raw: Any) -> str:
    """Normalise a source's chart indicator. Anything not explicit is 'unknown'."""
    if raw is None:
        return "unknown"
    if isinstance(raw, bool):
        return "prepared" if raw else "not_prepared"
    text = str(raw).strip().lower().replace("_", " ")
    if text in {"prepared", "chart prepared", "final chart prepared", "yes", "true"}:
        return "prepared"
    if text in {"not prepared", "chart not prepared", "no", "false"}:
        return "not_prepared"
    return "unknown"


def normalize_passengers(passengers: list[dict] | None) -> list[dict]:
    """Keep only non-identifying status fields for each passenger."""
    out = []
    for idx, p in enumerate(passengers or [], start=1):
        booking = (p.get("booking_status") or "").strip() or None
        current = (p.get("current_status") or "").strip() or None
        coach, berth = p.get("coach"), p.get("berth")
        if not coach:
            coach, berth = split_coach_berth(current)
        out.append(
            {
                "serial": int(p.get("serial") or idx),
                "booking_status": booking,
                "booking_code": status_code(booking),
                "current_status": current,
                "current_code": status_code(current),
                "coach": coach,
                "berth": berth,
            }
        )
    return out


def normalize_pnr_data(data: dict) -> dict:
    return {
        "train_number": _s(data.get("train_number")),
        "train_name": _s(data.get("train_name")),
        "journey_date": _date(data.get("journey_date")),
        "from_station_code": _s(data.get("from_station_code")),
        "to_station_code": _s(data.get("to_station_code")),
        "boarding_station_code": _s(data.get("boarding_station_code")),
        "travel_class": _s(data.get("travel_class")),
        "chart_status": chart_status(data.get("chart_status")),
        "passengers": normalize_passengers(data.get("passengers")),
    }


def _s(v: Any) -> str | None:
    return str(v).strip().upper() if v not in (None, "") else None


def _date(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_dt(v: Any) -> datetime | None:
    """Parse an ISO-8601 datetime to naive UTC. Naive input is assumed to be IST."""
    if not v:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def to_int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
