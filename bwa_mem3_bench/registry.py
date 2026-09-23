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

# What an entry's `expected_drift_pct` is a budget FOR -- i.e. which observed
# number Gate #1 compares it against.
#
# - `concordance`: 100 - compare-bams' `concordance_pct`, every scored field of
#   every primary. Right when the other side is the same kind of aligner
#   (upstream bwa-mem2), where near-identity is the expectation.
# - `confident_relocation`: compare-bams' `placement.relocated_pct`, the share of
#   primaries that at least one side maps with confidence which land at a
#   different locus. Right when the other side is a DIFFERENT aligner (bwameth),
#   where most field differences carry no placement information and the
#   headline concordance is dominated by candidate-set tags and repeat choice.
CONCORDANCE = "concordance"
CONFIDENT_RELOCATION = "confident_relocation"
METRICS = (CONCORDANCE, CONFIDENT_RELOCATION)


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
    # Which observed number the budget applies to; one of METRICS.
    metric: str = CONCORDANCE


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
        metric = str(item.get("metric", CONCORDANCE))
        if metric not in METRICS:
            raise ValueError(
                f"registry entry {i}: 'metric' must be one of {METRICS}, got {metric!r}"
            )
        entries.append(
            DivergenceEntry(
                id=str(item["id"]),
                pr=str(item["pr"]),
                date=str(item["date"]),
                summary=str(item["summary"]),
                affected=str(item["affected"]),
                expected_drift_pct=float(item["expected_drift_pct"]),
                samples=_coerce_samples(item.get("samples"), entry_index=i),
                metric=metric,
            )
        )
    return entries


def _applies_to(entry: DivergenceEntry, sample: str) -> bool:
    return not entry.samples or sample in entry.samples


def gate_metric(entries: list[DivergenceEntry], sample: str) -> str:
    """Which observed number Gate #1 scores ``sample`` on.

    `confident_relocation` when any entry that NAMES the sample declares it,
    else `concordance`. Only sample-scoped entries count: a corpus-wide entry
    (empty ``samples``) declaring `confident_relocation` would silently switch
    every sample off concordance, including ones deliberately gated at zero.
    """
    scoped = [e for e in entries if e.samples and sample in e.samples]
    if any(e.metric == CONFIDENT_RELOCATION for e in scoped):
        return CONFIDENT_RELOCATION
    return CONCORDANCE


def allowed_drift_pct(
    entries: list[DivergenceEntry], sample: str, metric: str = CONCORDANCE
) -> float:
    """Total drift budget (percent) declared for ``sample`` on ``metric``.

    Sums every registry entry on that metric that either applies to all samples
    (empty ``samples``) or explicitly lists ``sample``. This per-sample ceiling
    is what the regression gate compares observed drift against — replacing a
    single flat threshold, which cannot accommodate workloads whose intentional
    drift differs by orders of magnitude. Budgets on different metrics measure
    different things and are never added together.
    """
    return sum(
        e.expected_drift_pct for e in entries if e.metric == metric and _applies_to(e, sample)
    )
