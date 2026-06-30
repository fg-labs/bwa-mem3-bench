"""Persistent storage for bwa-mem3-bench benchmark runs (SQLite)."""

VS_BASELINE = "vs-baseline"
VS_GOLDEN = "vs-golden"
# `--fast`-preset concordance: a `<base>-fast` arm vs its default sibling
# (fg-labs/bwa-mem3 PR #189). Produced by the `compare_vs_default` rule and the
# opt-in `fast` target; stored as a `comparisons.kind` like the others.
VS_DEFAULT = "vs-default"
