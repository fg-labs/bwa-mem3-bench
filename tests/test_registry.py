"""Tests for the expected-divergences registry loader."""

from pathlib import Path

import pytest

from bwa_mem3_bench.registry import DivergenceEntry, load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "docs" / "expected-divergences.yaml"


def test_empty_registry_loads() -> None:
    entries = load_registry(REGISTRY)
    assert entries == []


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


def test_registry_missing_required_field_raises(tmp_path: Path) -> None:
    p = tmp_path / "reg.yaml"
    p.write_text("divergences: [{id: FG-001}]\n")
    with pytest.raises(ValueError, match="summary"):
        load_registry(p)
