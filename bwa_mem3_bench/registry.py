"""Expected-divergences registry: load YAML into typed records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DivergenceEntry:
    id: str
    pr: str
    date: str
    summary: str
    affected: str
    expected_drift_pct: float


_REQUIRED_FIELDS = ("id", "pr", "date", "summary", "affected", "expected_drift_pct")


def load_registry(path: Path) -> list[DivergenceEntry]:
    """Load and validate the expected-divergences registry from YAML."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    entries_raw = raw.get("divergences") or []

    entries: list[DivergenceEntry] = []
    for i, item in enumerate(entries_raw):
        missing = [f for f in _REQUIRED_FIELDS if f not in item]
        if missing:
            raise ValueError(f"registry entry {i} missing required field(s): {', '.join(missing)}")
        entries.append(
            DivergenceEntry(
                id=str(item["id"]),
                pr=str(item["pr"]),
                date=str(item["date"]),
                summary=str(item["summary"]),
                affected=str(item["affected"]),
                expected_drift_pct=float(item["expected_drift_pct"]),
            )
        )
    return entries
