"""Expected-divergences registry: load YAML into typed records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Repo-root-relative path to the shipped divergence registry. registry.py lives
# at bwa_mem3_bench/registry.py, so parent.parent is the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = _REPO_ROOT / "docs" / "expected-divergences.yaml"


@dataclass(frozen=True)
class DivergenceEntry:
    id: str
    pr: str
    date: str
    summary: str
    affected: str
    expected_drift_pct: float
    # Samples this entry's drift budget applies to. Empty means "all samples".
    # Drift magnitude varies enormously by workload (e.g. wes-5M ~0.0004% vs
    # meth ~1.1%), so the gate sums only the entries scoped to each sample.
    samples: tuple[str, ...] = ()


_REQUIRED_FIELDS = ("id", "pr", "date", "summary", "affected", "expected_drift_pct")


def _coerce_samples(raw: Any, *, entry_index: int) -> tuple[str, ...]:
    """Normalize the optional ``samples`` field to a tuple of strings.

    A YAML scalar (``samples: wgs-5M``) must become ``("wgs-5M",)`` — not be
    iterated character-by-character into ``("w", "g", ...)``, which would silently
    break Gate #1 sample matching.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(s) for s in raw)
    raise ValueError(f"registry entry {entry_index}: 'samples' must be a string or list")


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
                samples=_coerce_samples(item.get("samples"), entry_index=i),
            )
        )
    return entries


def allowed_drift_pct(entries: list[DivergenceEntry], sample: str) -> float:
    """Total drift budget (percent) declared for ``sample``.

    Sums every registry entry that either applies to all samples (empty
    ``samples``) or explicitly lists ``sample``. This per-sample ceiling is what
    the regression gate compares observed concordance drift against — replacing
    a single flat concordance threshold, which cannot accommodate workloads
    whose intentional drift differs by orders of magnitude.
    """
    return sum(e.expected_drift_pct for e in entries if not e.samples or sample in e.samples)
