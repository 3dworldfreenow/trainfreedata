"""Persistent system log (``system_logs`` table) written on its own connection.

Writing on a separate connection means log rows survive even if the caller's
transaction is rolled back, and a logging failure never propagates.
"""
from __future__ import annotations

import logging
from typing import Any

from .extensions import db
from .models import SystemLog, utcnow

_pylog = logging.getLogger("railmon")
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def log_event(level: str, component: str, message: str, **context: Any) -> None:
    level = level.upper() if level.upper() in _LEVELS else "INFO"
    _pylog.log(getattr(logging, level), "[%s] %s %s", component, message, context or "")
    try:
        with db.engine.begin() as conn:
            conn.execute(
                SystemLog.__table__.insert().values(
                    created_at=utcnow(),
                    level=level,
                    component=component[:64],
                    message=str(message)[:5000],
                    context=context or None,
                )
            )
    except Exception as exc:  # noqa: BLE001 - logging must never raise
        _pylog.error("could not persist system log: %s", exc)
