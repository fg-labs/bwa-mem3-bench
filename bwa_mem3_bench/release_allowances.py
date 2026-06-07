"""Release-to-release alignment-change allowances (Gate #2 sign-off).

bwa-mem3's output is gated *tightly* against the last blessed release (Gate #2):
two builds should be ~identical unless we deliberately change alignments. When
we do, the change must be signed off **two ways** — re-blessing a new golden
*and* a written allowance here recording the intentional change. `bless-golden`
refuses to move the golden to a new SHA unless an allowance authorizes it, so a
baseline move is never silent.

This is distinct from `expected-divergences.yaml`, which budgets drift vs
*upstream bwa-mem2* (Gate #1). Allowances here budget drift vs our *own previous
release*.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWANCES_PATH = _REPO_ROOT / "docs" / "release-allowances.yaml"


@dataclass(frozen=True)
class ReleaseAllowance:
    """One authorized, intentional alignment change moving the golden forward."""

    to_sha: str  # the fg-labs SHA being blessed as the new golden
    pr: str
    date: str
    summary: str
    expected_drift_pct: float


_REQUIRED_FIELDS = ("to_sha", "pr", "date", "summary", "expected_drift_pct")


def load_allowances(path: Path) -> list[ReleaseAllowance]:
    """Load and validate the release-allowances registry from YAML."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    entries_raw = raw.get("allowances") or []

    entries: list[ReleaseAllowance] = []
    for i, item in enumerate(entries_raw):
        missing = [f for f in _REQUIRED_FIELDS if f not in item]
        if missing:
            raise ValueError(
                f"release-allowance entry {i} missing required field(s): {', '.join(missing)}"
            )
        entries.append(
            ReleaseAllowance(
                to_sha=str(item["to_sha"]),
                pr=str(item["pr"]),
                date=str(item["date"]),
                summary=str(item["summary"]),
                expected_drift_pct=float(item["expected_drift_pct"]),
            )
        )
    return entries


def allowance_for(entries: list[ReleaseAllowance], to_sha: str) -> ReleaseAllowance | None:
    """The allowance authorizing a bless of ``to_sha``, or None if undeclared.

    Matches on a SHA prefix in either direction so a short tag SHA and a full
    SHA interoperate (mirrors how the rest of the CLI accepts abbreviated SHAs).
    Fails closed for the Gate #2 authorization guarantee: an empty query never
    matches, and an ambiguous query (matching more than one entry) raises rather
    than authorizing on an arbitrary first hit.
    """
    q = to_sha.strip()
    if not q:
        return None
    matches = [
        e for e in entries if e.to_sha and (q.startswith(e.to_sha) or e.to_sha.startswith(q))
    ]
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous release-allowance match for {to_sha!r}: "
            f"{', '.join(e.to_sha for e in matches)} — use a longer SHA"
        )
    return matches[0] if matches else None
