"""Adapter registry: maps ``api_sources.adapter`` names to classes."""
from __future__ import annotations

from .base import BaseAdapter
from .generic_http import GenericHttpAdapter
from .official_manual import IndianRailPnrManualAdapter, ManualEntryAdapter, NtesManualAdapter
from .static_timetable import StaticTimetableAdapter

REGISTRY: dict[str, type[BaseAdapter]] = {
    cls.adapter_name: cls
    for cls in (
        StaticTimetableAdapter,
        NtesManualAdapter,
        IndianRailPnrManualAdapter,
        ManualEntryAdapter,
        GenericHttpAdapter,
    )
}


def register_adapter(cls: type[BaseAdapter]) -> type[BaseAdapter]:
    """Decorator for third-party adapters: ``@register_adapter class MyAdapter(BaseAdapter): ...``"""
    REGISTRY[cls.adapter_name] = cls
    return cls
