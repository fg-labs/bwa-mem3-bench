"""Tests for the expected-divergences registry loader."""

from pathlib import Path

import pytest

from bwa_mem3_bench.registry import DivergenceEntry, allowed_drift_pct, load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "docs" / "expected-divergences.yaml"


def test_shipped_registry_loads_and_is_valid() -> None:
    """The repo's registry parses, is non-empty, and every entry is well-formed."""
    entries = load_registry(REGISTRY)
    assert entries, "shipped registry should declare the known divergences"
    for e in entries:
        assert e.id and e.pr and e.summary and e.affected
        assert e.expected_drift_pct >= 0.0


def test_registry_parses_entries(tmp_path: Path) -> None:
    p = tmp_path / "reg.yaml"
    p.write_text(
        """
divergences:
  - id: FG-001
    pr: fg-labs/bwa-mem3#12
    date: 2025-11-03
    summary: "test fix"
    affected: secondary_alignments
    expected_drift_pct: 0.03
""".strip()
    )
    entries = load_registry(p)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, DivergenceEntry)
    assert entry.id == "FG-001"
    assert entry.pr == "fg-labs/bwa-mem3#12"
    assert entry.affected == "secondary_alignments"
    assert entry.expected_drift_pct == pytest.approx(0.03)
    assert entry.samples == ()  # omitted -> applies to all samples


def test_registry_missing_required_field_raises(tmp_path: Path) -> None:
    p = tmp_path / "reg.yaml"
    p.write_text("divergences: [{id: FG-001}]\n")
    with pytest.raises(ValueError, match="summary"):
        load_registry(p)


def test_registry_scalar_samples_becomes_single_tuple(tmp_path: Path) -> None:
    # A YAML scalar must NOT be iterated character-by-character.
    p = tmp_path / "reg.yaml"
    p.write_text(
        """
divergences:
  - id: FG-001
    pr: p
    date: 2026-06-07
    summary: s
    affected: primary_alignment
    expected_drift_pct: 0.1
    samples: wgs-5M
""".strip()
    )
    entry = load_registry(p)[0]
    assert entry.samples == ("wgs-5M",)


def test_registry_invalid_samples_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "reg.yaml"
    p.write_text(
        """
divergences:
  - id: FG-001
    pr: p
    date: 2026-06-07
    summary: s
    affected: primary_alignment
    expected_drift_pct: 0.1
    samples: 42
""".strip()
    )
    with pytest.raises(ValueError, match="samples"):
        load_registry(p)


def _entry(id_: str, drift: float, samples: tuple[str, ...]) -> DivergenceEntry:
    return DivergenceEntry(
        id=id_,
        pr="fg-labs/bwa-mem3#1",
        date="2026-06-07",
        summary="s",
        affected="primary_alignment",
        expected_drift_pct=drift,
        samples=samples,
    )


def test_allowed_drift_sums_scoped_and_global_entries() -> None:
    entries = [
        _entry("GLOBAL", 0.01, ()),  # applies everywhere
        _entry("WGS", 0.10, ("wgs-5M",)),  # scoped
        _entry("METH", 1.50, ("meth-twist-emseq-5M",)),
    ]
    # global + wgs-scoped
    assert allowed_drift_pct(entries, "wgs-5M") == pytest.approx(0.11)
    # only global applies to an unscoped sample
    assert allowed_drift_pct(entries, "panel-twist-5M") == pytest.approx(0.01)
    # global + meth-scoped
    assert allowed_drift_pct(entries, "meth-twist-emseq-5M") == pytest.approx(1.51)


def test_shipped_registry_budgets_cover_observed_bffae5a_drift() -> None:
    """Declared budgets must exceed the drift measured at bffae5a."""
    entries = load_registry(REGISTRY)
    # observed drift (100 - concordance) at bffae5a
    observed = {
        "wes-5M": 0.0004,
        "wgs-5M": 0.0107,
        "panel-twist-5M": 0.0586,
        "smoke-1M": 0.054,
        "meth-twist-emseq-5M": 1.1227,
        "smoke-meth": 1.1571,
    }
    for sample, drift in observed.items():
        assert allowed_drift_pct(entries, sample) >= drift, sample
