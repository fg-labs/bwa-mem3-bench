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
    # Additional SHAs that are code-identical to `to_sha` and share its golden,
    # so no BAMs need re-copying. The canonical use: a release benched on its
    # release-please branch head (`to_sha`) then squash-merged to a different tag
    # SHA — the two trees are identical, so the tag is listed here and resolves
    # to `to_sha`'s golden. Empty for the common single-SHA case.
    aliases: tuple[str, ...] = ()


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
        aliases_raw = item.get("aliases") or []
        # A bare scalar (`aliases: <sha>`, missing the list dash) would otherwise
        # be iterated character-by-character into a bogus alias tuple, silently
        # breaking alias matching. Require an explicit list and fail fast.
        if not isinstance(aliases_raw, list):
            raise ValueError(
                f"release-allowance entry {i}: 'aliases' must be a list of SHAs; got "
                f"{aliases_raw!r}"
            )
        entries.append(
            ReleaseAllowance(
                to_sha=str(item["to_sha"]),
                pr=str(item["pr"]),
                date=str(item["date"]),
                summary=str(item["summary"]),
                expected_drift_pct=float(item["expected_drift_pct"]),
                aliases=tuple(str(a) for a in aliases_raw),
            )
        )
    return entries


def _sha_prefix_match(a: str, b: str) -> bool:
    """True when non-empty ``a`` and ``b`` are prefix-compatible (either is a
    prefix of the other) — mirrors the CLI's tolerance of abbreviated SHAs."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _entry_matches(entry: ReleaseAllowance, query: str) -> bool:
    """True when ``query`` prefix-matches the entry's ``to_sha`` or any alias."""
    return _sha_prefix_match(entry.to_sha, query) or any(
        _sha_prefix_match(a, query) for a in entry.aliases
    )


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
    matches = [e for e in entries if _entry_matches(e, q)]
    if len(matches) > 1:
        raise ValueError(
            f"ambiguous release-allowance match for {to_sha!r}: "
            f"{', '.join(e.to_sha for e in matches)} — use a longer SHA"
        )
    return matches[0] if matches else None


def canonical_golden_sha(entries: list[ReleaseAllowance], query: str) -> str:
    """The SHA under which the golden for ``query`` physically lives.

    If ``query`` matches an allowance's ``to_sha`` or one of its ``aliases``,
    return that entry's ``to_sha`` (the key ``bless-golden`` stored the BAMs
    under). Otherwise return ``query`` unchanged — a SHA with no alias entry is
    its own golden key, so existing (unaliased) goldens resolve exactly as
    before. This lets a release referenced by a code-identical alias (e.g. a
    squash-merged tag) reuse the golden blessed under the benched SHA without
    re-copying any BAMs.
    """
    match = allowance_for(entries, query)
    return match.to_sha if match is not None else query.strip()
