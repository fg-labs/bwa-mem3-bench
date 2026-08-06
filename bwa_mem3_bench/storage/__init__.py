"""Persistent storage for bwa-mem3-bench benchmark runs (SQLite)."""

VS_BASELINE = "vs-baseline"
VS_GOLDEN = "vs-golden"
# ARM fg-labs vs x86 fg-labs. Upstream bwa-mem2 has no ARM SIMD, so an ARM cell
# cannot be scored against it directly; `rule all` sends ARM to this kind and
# the concordance chain closes through the x86 arm's own `vs-baseline`.
VS_X86 = "vs-x86"
# `--fast`-preset concordance: a `<base>-fast` arm vs its default sibling
# (fg-labs/bwa-mem3 PR #189). Produced by the `compare_vs_default` rule and the
# opt-in `fast` target; stored as a `comparisons.kind` like the others.
VS_DEFAULT = "vs-default"
