"""Load samples + archs + defaults from YAML into typed records."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The `compare-bams` invocations the workflow makes, one per rule in
# `workflow/rules/compare.smk`. Three flavours, not two:
#   - `vs_baseline` — a DIFFERENT aligner (upstream bwa-mem2, or bwameth for
#     meth samples), so it needs the largest exclusion list.
#   - `vs_golden` / `vs_x86` — same binary AND same search settings, so every
#     tag is comparable and nothing is skipped.
#   - `vs_default` — same binary but preset-pruned (`--fast` vs default), so the
#     tags describing the candidate set diverge mechanically and are excluded
#     while the tags describing the chosen alignment stay strict.
COMPARE_KINDS = frozenset({"vs_baseline", "vs_golden", "vs_x86", "vs_default"})

# Mate tags. A single-end read has no mate, so these cannot exist on one -- a
# logical impossibility, not an aligner choice. Measured: `sbx-1M` carries
# neither on either side of any comparison.
MATE_ONLY_TAGS = frozenset({"MQ", "MC"})

# Tags neither side emits under `--meth`. `bwa-mem3`'s methylation output goes
# through a separate writer (`src/meth_bam.cpp`) that omits MQ and HN, and
# bwameth emits neither either. Measured: 0 of 10,369,692 primaries on
# `meth-twist-emseq-5M`, likewise on `smoke-meth`.
#
# Unlike MATE_ONLY_TAGS this is a defect, not a law: tracked as
# fg-labs/bwa-mem3#296. DELETE THIS CONSTANT when that lands. Note it only
# exempts the two tags from the dead-entry audit -- they stay on `ignore_tags`,
# because bwameth will still never emit them once bwa-mem3 does.
METH_UNEMITTED_TAGS = frozenset({"MQ", "HN"})

# Tags that appear only on methylation comparisons: XM/XG/XR from `bwa-mem3
# --meth`, and YD/YC/RG from bwameth. Derived rather than declared per sample
# because it is one fact about bisulfite alignment, and restating it across ~10
# meth samples x 3 comparison kinds invites exactly the drift this guard exists
# to catch.
METH_EXTRA_TAGS = frozenset({"XM", "XG", "XR", "YD", "YC", "RG"})


@dataclass(frozen=True)
class Sample:
    name: str
    baseline_tool: str
    reference: str
    source: str
    # "paired" (r1+r2 FASTQs) or "single" (r1 only, e.g. single-end SBX reads).
    layout: str = "paired"
    fg_labs_flags: list[str] = field(default_factory=list)
    # `mem` flags applied to BOTH the fg-labs and the upstream-baseline `mem`
    # invocations (NOT bwameth). Unlike `fg_labs_flags` (fg-labs-only), these
    # change the alignment and so must go to both sides to keep concordance
    # apples-to-apples. Used by `hic-1M` for canonical Hi-C flags (`-5 -S -P`),
    # which disable the (Hi-C-inappropriate) mate rescue that otherwise blows up
    # the mate-SW reference windows and OOMs the cgroup.
    mem_flags: list[str] = field(default_factory=list)
    # Per-comparison-kind compare-bams overrides, keyed by kind (see
    # `COMPARE_KINDS`), e.g. `{"vs_baseline": {"ignore_tags": [...]}}`. Each
    # kind's `ignore_tags` EXTENDS the matching `compare_defaults` entry rather
    # than replacing it. Resolve via `WorkflowConfig.ignore_tags()` — never read
    # this directly, or the defaults get silently dropped.
    compare_options: dict[str, Any] = field(default_factory=dict)
    # Truth-based accuracy sample (holodeck-simulated). When True, the sample's
    # S3 `source` prefix also holds the truth artifacts (`golden.bam`,
    # `truth.vcf`, and for meth samples `cpg-truth.bedGraph`) that the
    # `eval_accuracy` rule grades the aligner BAM against. Truth samples are
    # driven by the `accuracy` / `accuracy_smoke` targets, NOT the speed/
    # concordance sweep (`rule all` / `baseline_all` exclude them).
    truth: bool = False

    def __post_init__(self) -> None:
        if self.layout not in ("paired", "single"):
            raise ValueError(
                f"sample {self.name!r} has invalid layout {self.layout!r}; "
                f"expected 'paired' or 'single'"
            )
        # Single-predicate invariant: a methylation sample MUST use a `-meth`
        # reference and a non-meth sample MUST NOT. The alignment rule stages
        # the reference index off `sample.reference` but passes the `--meth`
        # exec flag off `is_meth` (baseline_tool / fg_labs_flags); if a
        # hand-edited config let those disagree, the pipeline would stage the
        # wrong index and pass mismatched args (missing `.0123` / bad seed index
        # at runtime). Enforce consistency here so the two can never diverge.
        if self.is_meth != self.reference.endswith("-meth"):
            raise ValueError(
                f"sample {self.name!r} has inconsistent methylation config: "
                f"is_meth={self.is_meth} (baseline_tool={self.baseline_tool!r}, "
                f"fg_labs_flags={self.fg_labs_flags}) but reference={self.reference!r}. "
                f"Meth samples must use a '-meth' reference and non-meth samples must not."
            )

    @property
    def is_meth(self) -> bool:
        """Single source of truth for whether this is a methylation
        (bisulfite/EM-seq) sample: the bwameth baseline or an fg-labs `--meth`
        flag. The workflow's meth predicates and the reference-staging branch
        all key off this, so reference selection and the `--meth` exec flag
        cannot disagree (enforced in `__post_init__`)."""
        return self.baseline_tool == "bwameth" or "--meth" in self.fg_labs_flags

    @property
    def fastq_names(self) -> tuple[str, ...]:
        """Ordered query-FASTQ basenames for this sample's layout.

        Paired -> (r1, r2); single-end -> (r1,). The align rules join these with
        ``source`` to build the ordered ``fastqs`` input list.
        """
        if self.layout == "single":
            return ("r1.fq.gz",)
        return ("r1.fq.gz", "r2.fq.gz")

    @property
    def minibwa_flags(self) -> list[str]:
        """`mem_flags` translated to their ``minibwa map`` equivalents.

        `mem_flags` are bwa-mem CLI flags applied to the bwa-mem2 / bwa-mem3
        arms. minibwa's CLI is mostly bwa-compatible but not identical, so the
        minibwa probe must run the *equivalent* flags rather than the bwa ones
        verbatim — otherwise the comparison is not apples-to-apples (e.g. Hi-C's
        mate rescue would stay ON for minibwa while it is OFF for the others).

        Translation (the only flags we use today):
          - ``-5`` / ``-P`` -> identical in minibwa (PRIMARY5 / NO_PAIRING).
          - ``-S`` (skip mate rescue) -> ``--rescue=0`` (minibwa has no ``-S``;
            mate rescue is a count, 0 disables it).

        An unrecognized flag raises rather than being silently dropped or passed
        through to a minibwa that would reject it — a new `mem_flags` entry must
        be given an explicit minibwa mapping here.
        """
        translated: list[str] = []
        for flag in self.mem_flags:
            if flag in ("-5", "-P"):
                translated.append(flag)
            elif flag == "-S":
                translated.append("--rescue=0")
            else:
                raise ValueError(
                    f"sample {self.name!r}: mem_flag {flag!r} has no minibwa "
                    f"equivalent mapping in Sample.minibwa_flags; add one"
                )
        return translated


@dataclass(frozen=True)
class Arch:
    name: str
    instance_type: str
    batch_queue: str
    simd: str
    platform: str
    # fg-labs/bwa-mem3 BASELINE_ARCH build-arg for this arch's image. Empty
    # string means "no override" (use the upstream default). See
    # config/archs.yaml for rationale.
    baseline_arch: str = ""

    def image_uri(self, *, ecr_repo_uri: str, fg_labs_sha: str) -> str:
        """Fully-qualified ECR image URI for this arch's worker jobs.

        Derived from `baseline_arch`:
          - empty string  -> ``<ECR>:<sha>``         (portable, multi-arch)
          - else          -> ``<ECR>:<sha>-<suffix>`` (host-locked variant)

        The build side (``cli build --baseline-arch <tier>``) produces the
        matching tag; both sides read this dataclass field so they stay in
        sync. The workflow's per-rule ``resources.container_image`` lambda
        calls into this method, and our snakemake-executor-plugin-aws-batch
        fork uses the resource as the SubmitJob job-def's
        ``containerProperties.image``.
        """
        tag = fg_labs_sha + (f"-{self.baseline_arch}" if self.baseline_arch else "")
        return f"{ecr_repo_uri}:{tag}"


@dataclass(frozen=True)
class ThreadScalingStep:
    """One rung of the thread-scaling ladder: a thread count and its replication.

    Replication is per-rung rather than global because cost is wildly uneven —
    a 1-thread run is ~16x the wall of a 16-thread run — while the high thread
    counts are the ones the regression gate reads.
    """

    threads: int
    reps: int


@dataclass(frozen=True)
class ThreadScaling:
    """Configuration for the thread-scaling ladder (`--target thread_scaling`).

    The whole ladder runs as ONE job on ONE host: strong-scaling efficiency
    ``E(n) = T(1) / (n * T(n))`` is only meaningful on fixed hardware, since
    different instance sizes get different shares of memory bandwidth and L3 —
    which is precisely what bounds bwa-mem's scaling.
    """

    sample: str
    arch: str
    ladder: list[ThreadScalingStep]
    max_efficiency_drop_pp: float

    @property
    def max_threads(self) -> int:
        """Largest thread count in the ladder — what the job must reserve."""
        return max(step.threads for step in self.ladder)


@dataclass(frozen=True)
class WorkflowConfig:
    samples: dict[str, Sample]
    archs: dict[str, Arch]
    core_arch: str
    full_archs: list[str]
    region: str
    bucket: str
    ecr_repo: str
    upstream_tag: str
    bwameth_version: str
    bwa_version: str
    threads: int
    reps_default: int
    reps_baseline: int
    thread_scaling: ThreadScaling
    references: dict[str, dict[str, str]]
    runs_prefix: str
    baseline_prefix: str
    golden_prefix: str
    data_prefix: str
    # Per-comparison-kind defaults, keyed by kind, each a mapping with
    # `ignore_tags` and `expect_tags` lists. Per-sample `compare_options` extend
    # these.
    compare_defaults: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def _resolve_tags(self, sample_name: str, kind: str, key: str) -> set[str]:
        """Union of a kind's default tag list and the sample's addition to it.

        Extending rather than replacing is the invariant both `ignore_tags` and
        `expect_tags` rely on: a meth sample needs both the cross-tool default
        and its own additions, and a config that replaced would silently drop
        the former.

        :param sample_name: sample being compared.
        :param kind: comparison kind, one of `COMPARE_KINDS`.
        :param key: which list to resolve -- `ignore_tags` or `expect_tags`.
        :return: the unioned tag names.
        :raises KeyError: if `sample_name` is not a configured sample.
        :raises ValueError: if `kind` is not a known comparison kind.
        """
        if kind not in COMPARE_KINDS:
            raise ValueError(
                f"unknown comparison kind {kind!r}; expected one of {sorted(COMPARE_KINDS)}"
            )
        tags = set(self.compare_defaults.get(kind, {}).get(key, []))
        override = self.samples[sample_name].compare_options.get(kind, {})
        tags.update(override.get(key, []))
        return tags

    def ignore_tags(self, sample_name: str, kind: str) -> list[str]:
        """Aux tags `compare-bams` must skip for one (sample, comparison kind).

        :param sample_name: sample being compared.
        :param kind: comparison kind, one of `COMPARE_KINDS`.
        :return: sorted, de-duplicated tag names.
        """
        return sorted(self._resolve_tags(sample_name, kind, "ignore_tags"))

    def expect_tags(self, sample_name: str, kind: str) -> list[str]:
        """Aux tags that MAY appear for one (sample, comparison kind).

        Any tag `compare-bams` observes that is on neither this list nor
        `ignore_tags` fails the run by name. The semantics are *may* appear, not
        *must*: a listed tag that never shows up is a harmless no-op, which is
        what lets one per-kind list serve samples whose tag sets legitimately
        differ without needing a per-sample subtraction.

        Methylation samples get `METH_EXTRA_TAGS` added automatically -- see that
        constant for why it is derived rather than declared.

        :param sample_name: sample being compared.
        :param kind: comparison kind, one of `COMPARE_KINDS`.
        :return: sorted, de-duplicated tag names.
        """
        tags = self._resolve_tags(sample_name, kind, "expect_tags")
        if self.samples[sample_name].is_meth:
            tags |= METH_EXTRA_TAGS
        return sorted(tags)

    def absent_ok_tags(self, sample_name: str, kind: str) -> list[str]:
        """`ignore_tags` entries known to be absent for one (sample, kind).

        These are exempt from `compare-bams`' dead-entry check, which otherwise
        fails a run whose `ignore_tags` names a tag matching no record. Two
        populations qualify: mate tags on single-end samples (impossible by
        definition) and MQ/HN on methylation samples (absent by defect --
        fg-labs/bwa-mem3#296).

        The result is intersected with the resolved `ignore_tags` because only
        ignore entries are ever audited; naming a tag that is not ignored would
        be inert config, which is the very thing this guard exists to reject.

        :param sample_name: sample being compared.
        :param kind: comparison kind, one of `COMPARE_KINDS`.
        :return: sorted, de-duplicated tag names.
        """
        sample = self.samples[sample_name]
        absent: set[str] = set()
        if sample.layout == "single":
            absent |= MATE_ONLY_TAGS
        if sample.is_meth:
            absent |= METH_UNEMITTED_TAGS
        return sorted(absent & self._resolve_tags(sample_name, kind, "ignore_tags"))


def _as_str_list(owner: str, key: str, value: Any) -> list[str]:
    """Validate a YAML flag value is a ``list[str]`` before coercion.

    ``list(...)`` on a bare YAML scalar (e.g. ``mem_flags: -5``) silently
    splits the string into characters (``['-', '5']``), corrupting the
    alignment arguments. Reject anything that is not already a list of
    strings so a misconfiguration fails loudly at load time.

    :param owner: what the key belongs to, rendered verbatim into the error
        message (e.g. ``"sample 'wgs-5M'"`` or ``"`compare_defaults`"``). Not
        every caller is a sample — the top-level `compare_defaults` block uses
        this helper too, and naming it a sample would misdirect the fix.
    :param key: config key being validated (e.g. ``"mem_flags"``).
    :param value: raw value read from YAML.
    :return: the value as a ``list[str]``.
    :raises ValueError: if ``value`` is not a list of strings.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{owner} `{key}` must be a list of strings; got {value!r}")
    return list(value)


def _as_bool(sample_name: str, key: str, value: Any) -> bool:
    """Validate a YAML flag value is a real ``bool`` before use.

    YAML's implicit typing accepts ``true``/``false`` as booleans, but a quoted
    or mistyped value (``truth: "yes"``, ``truth: 1``) would be silently coerced
    to ``True`` by ``bool(...)``. Reject anything that is not already a bool so a
    misconfiguration fails loudly at load time (mirrors ``_as_str_list``).

    :param sample_name: sample the flag belongs to (for the error message).
    :param key: config key being validated (e.g. ``"truth"``).
    :param value: raw value read from YAML.
    :return: the value as a ``bool``.
    :raises ValueError: if ``value`` is not a bool.
    """
    if not isinstance(value, bool):
        raise ValueError(f"sample {sample_name!r} `{key}` must be a boolean; got {value!r}")
    return value


def _as_positive_int(context: str, key: str, value: Any) -> int:
    """Validate a YAML value is a real ``int`` >= 1 before use.

    ``int(...)`` would silently accept anything int-like: it truncates a
    fractional value (``threads: 16.9`` → ``16``), parses a quoted string, and
    passes a bool straight through (``reps: true`` → ``1``). Reject anything that
    is not already a positive int so a misconfiguration fails loudly at load time
    (mirrors ``_as_bool`` / ``_as_str_list``).

    :param context: what the value belongs to, for the error message.
    :param key: config key being validated (e.g. ``"threads"``).
    :param value: raw value read from YAML.
    :return: the value as an ``int``.
    :raises ValueError: if ``value`` is not an int, or is < 1.
    """
    # bool subclasses int, so it has to be excluded explicitly.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{context} `{key}` must be an integer >= 1; got {value!r}")
    return value


_LADDER_TOKEN_FIELDS = 2  # `threads:reps`


def parse_ladder_override(spec: str) -> list[ThreadScalingStep]:
    """Parse an ad-hoc ladder given as ``threads:reps`` tokens, e.g. ``16:3,64:3``.

    Used for `--config ladder=...` (see `workflow/rules/scaling.smk`), which
    bypasses `defaults.yaml` entirely. The tokens are interpolated straight into
    the rule's shell loop, so they get the same validation the checked-in ladder
    does — an unvalidated `16:` or `sixteen:3` would otherwise reach the worker
    and fail an hour into a spot job, or silently run the wrong rung.

    Unlike the checked-in ladder this does NOT require a 1-thread rung: skipping
    it is the point of the override (it alone is ~40% of the full ladder's wall
    time). The result yields no efficiency, so Gate #3 no-ops on it.

    :param spec: comma-separated ``threads:reps`` tokens; surrounding whitespace
        and empty tokens are ignored.
    :return: the parsed rungs, ordered by thread count.
    :raises ValueError: if `spec` holds no rungs, a token is not exactly one
        ``threads:reps`` pair, a value is not an integer >= 1, or a thread count
        repeats.
    """
    steps: list[ThreadScalingStep] = []
    for token in (tok.strip() for tok in spec.split(",")):
        if not token:
            continue
        parts = [part.strip() for part in token.split(":")]
        # isdigit() also rejects signs and decimal points, so the int() below
        # cannot raise and cannot truncate.
        if len(parts) != _LADDER_TOKEN_FIELDS or not all(part.isdigit() for part in parts):
            raise ValueError(
                f"ladder override token {token!r} must be `threads:reps`, both integers"
            )
        threads, reps = int(parts[0]), int(parts[1])
        if threads < 1 or reps < 1:
            raise ValueError(f"ladder override token {token!r} needs threads >= 1 and reps >= 1")
        steps.append(ThreadScalingStep(threads=threads, reps=reps))
    if not steps:
        raise ValueError(f"ladder override {spec!r} contains no `threads:reps` rungs")
    counts = [step.threads for step in steps]
    if len(set(counts)) != len(counts):
        raise ValueError(f"ladder override {spec!r} repeats a thread count: {counts}")
    return sorted(steps, key=lambda step: step.threads)


def _validate_compare_options(sample_name: str, options: Any) -> dict[str, Any]:
    """Validate a sample's `compare_options` is keyed by comparison kind.

    `compare_options` was once a flat `{"ignore_tags": [...]}` that applied to
    every comparison — and, for the whole of this project's history, to none of
    them, because nothing read it (bench #34). Rejecting the flat shape means a
    config carried over from that era fails at load instead of quietly reverting
    to the defaults, which is the exact failure mode that bug was.

    :param sample_name: sample the options belong to (for the error message).
    :param options: raw `compare_options` value from YAML. Typed `Any` because
        the caller passes it unconverted: a bare `compare_options:` header
        yields `None` and a mistyped one yields a list or string, and coercing
        those with `dict(...)` before the guard raises `TypeError` /
        "dictionary update sequence" instead of naming the sample. Mirrors
        `_validate_compare_defaults`.
    :return: the validated mapping.
    :raises ValueError: if the value is not a mapping, is not keyed by a known
        comparison kind, or a kind's body is not a mapping.
    """
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError(
            f"sample {sample_name!r} `compare_options` must be a mapping keyed by "
            f"comparison kind (e.g. `{{vs_baseline: {{ignore_tags: [NM]}}}}`); "
            f"got {options!r}"
        )
    for key, body in options.items():
        if key == "ignore_tags":
            raise ValueError(
                f"sample {sample_name!r} uses the retired flat "
                f"`compare_options.ignore_tags`. Tag policy is now per comparison "
                f"kind: nest it under the kind it applies to, e.g.\n"
                f"    compare_options:\n"
                f"      vs_baseline:\n"
                f"        ignore_tags: {body!r}"
            )
        if key not in COMPARE_KINDS:
            raise ValueError(
                f"sample {sample_name!r} `compare_options` has unknown comparison "
                f"kind {key!r}; expected one of {sorted(COMPARE_KINDS)}"
            )
        if not isinstance(body, dict):
            raise ValueError(
                f"sample {sample_name!r} `compare_options.{key}` must be a mapping "
                f"(e.g. `{{ignore_tags: [NM]}}`); got {body!r}"
            )
        for list_key in ("ignore_tags", "expect_tags"):
            _as_str_list(
                f"sample {sample_name!r}",
                f"compare_options.{key}.{list_key}",
                body.get(list_key, []),
            )
    return options


def _validate_compare_defaults(raw: Any) -> dict[str, dict[str, list[str]]]:
    """Validate the top-level `compare_defaults` block and flatten it to lists.

    Every known kind must declare a NON-EMPTY `expect_tags`. That requirement is
    what makes `compare-bams`' unexpected-tag check enforceable: the binary skips
    that check when handed no allowlist, because an unconfigured allowlist is
    indistinguishable from an empty one and failing every tag would be useless.
    Requiring it here means a new comparison kind cannot be added with the guard
    silently inert -- which is bench #34's failure mode exactly.

    :param raw: the raw `compare_defaults` mapping from `samples.yaml`.
    :return: kind -> {`ignore_tags`, `expect_tags`}, with every known kind present.
    :raises ValueError: on an unknown kind, a non-mapping body, a list that is
        not a list of strings, or a kind with no `expect_tags`.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"`compare_defaults` must be a mapping; got {raw!r}")
    out: dict[str, dict[str, list[str]]] = {
        kind: {"ignore_tags": [], "expect_tags": []} for kind in COMPARE_KINDS
    }
    for kind, body in raw.items():
        if kind not in COMPARE_KINDS:
            raise ValueError(
                f"`compare_defaults` has unknown comparison kind {kind!r}; "
                f"expected one of {sorted(COMPARE_KINDS)}"
            )
        if not isinstance(body, dict):
            raise ValueError(
                f"`compare_defaults.{kind}` must be a mapping "
                f"(e.g. `{{ignore_tags: [MQ, HN]}}`); got {body!r}"
            )
        out[kind] = {
            list_key: _as_str_list(
                "`compare_defaults`", f"{kind}.{list_key}", body.get(list_key, [])
            )
            for list_key in ("ignore_tags", "expect_tags")
        }

    missing = sorted(kind for kind, body in out.items() if not body["expect_tags"])
    if missing:
        raise ValueError(
            f"`compare_defaults` must declare a non-empty `expect_tags` for every "
            f"comparison kind; missing for {missing}. Without it compare-bams "
            f"cannot enforce its unexpected-tag check, so a tag nobody anticipated "
            f"would show up only as an unexplained drop in concordance. List the "
            f"tags the two sides may emit, e.g.\n"
            f"    compare_defaults:\n"
            f"      {missing[0]}:\n"
            f"        expect_tags: [AS, HN, MC, MD, MQ, NM, SA, XA, XS]"
        )
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r") as fh:
        result: dict[str, Any] = yaml.safe_load(fh)
        return result


def _thread_scaling_from(
    raw: Any, *, samples: dict[str, Sample], archs: dict[str, Arch]
) -> ThreadScaling:
    """Validate and build the `thread_scaling` block from `defaults.yaml`.

    Fails loudly at load time rather than mid-run: the ladder drives a single
    long Batch job, so a typo here would otherwise surface as a failed job an
    hour in.

    :param raw: the `thread_scaling` mapping read from YAML.
    :param samples: parsed samples, to check the referenced sample exists.
    :param archs: parsed archs, to check the referenced arch exists.
    :return: the validated `ThreadScaling`.
    :raises ValueError: on a missing key, unknown sample/arch, malformed ladder,
        a ladder without a 1-thread rung, duplicate thread counts, a
        threads/reps value that is not an integer >= 1, or a
        `max_efficiency_drop_pp` that is not a finite number >= 0.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"`thread_scaling` must be a mapping; got {raw!r}")
    for key in ("sample", "arch", "ladder", "max_efficiency_drop_pp"):
        if key not in raw:
            raise ValueError(f"`thread_scaling` is missing required key {key!r}")

    sample, arch = raw["sample"], raw["arch"]
    if sample not in samples:
        raise ValueError(f"`thread_scaling.sample` {sample!r} is not a configured sample")
    if arch not in archs:
        raise ValueError(f"`thread_scaling.arch` {arch!r} is not a configured arch")

    raw_ladder = raw["ladder"]
    if not isinstance(raw_ladder, list) or not raw_ladder:
        raise ValueError(f"`thread_scaling.ladder` must be a non-empty list; got {raw_ladder!r}")

    ladder: list[ThreadScalingStep] = []
    for entry in raw_ladder:
        if not isinstance(entry, dict) or "threads" not in entry or "reps" not in entry:
            raise ValueError(
                f"each `thread_scaling.ladder` entry needs `threads` and `reps`; got {entry!r}"
            )
        where = f"`thread_scaling.ladder` entry {entry!r}"
        ladder.append(
            ThreadScalingStep(
                threads=_as_positive_int(where, "threads", entry["threads"]),
                reps=_as_positive_int(where, "reps", entry["reps"]),
            )
        )

    counts = [step.threads for step in ladder]
    if len(set(counts)) != len(counts):
        raise ValueError(f"`thread_scaling.ladder` has duplicate thread counts: {counts}")
    # E(n) = T(1) / (n * T(n)) is undefined without a single-thread measurement,
    # and the gate reads efficiency, so a ladder missing the 1-thread rung would
    # produce a job whose output cannot be scored.
    if 1 not in counts:
        raise ValueError(
            f"`thread_scaling.ladder` must include a 1-thread rung (the T(1) baseline "
            f"every efficiency is computed against); got thread counts {sorted(counts)}"
        )

    raw_drop = raw["max_efficiency_drop_pp"]
    # Same reasoning as `_as_positive_int`: `float(...)` would take a bool or a
    # quoted string, and this value is a gate tolerance — a silently coerced one
    # gates the release against the wrong number. `nan`/`inf` need the explicit
    # `isfinite` check because both are numeric and neither is `< 0`: `nan` makes
    # every comparison against it false (the gate never fires) and `inf` makes the
    # tolerance unbounded (the gate can never fail). Either one silently disables
    # Gate #3 rather than loosening it.
    if (
        not isinstance(raw_drop, (int, float))
        or isinstance(raw_drop, bool)
        or not math.isfinite(raw_drop)
        or raw_drop < 0
    ):
        raise ValueError(
            f"`thread_scaling.max_efficiency_drop_pp` must be a finite number >= 0; "
            f"got {raw_drop!r}"
        )
    drop = float(raw_drop)

    return ThreadScaling(
        sample=sample,
        arch=arch,
        ladder=sorted(ladder, key=lambda s: s.threads),
        max_efficiency_drop_pp=drop,
    )


def load_config(config_dir: Path) -> WorkflowConfig:
    """Load and validate the three-file config into a `WorkflowConfig`."""
    samples_yaml = _read_yaml(config_dir / "samples.yaml")
    samples_raw = samples_yaml["samples"]
    compare_defaults = _validate_compare_defaults(samples_yaml.get("compare_defaults"))
    archs_raw = _read_yaml(config_dir / "archs.yaml")
    defaults = _read_yaml(config_dir / "defaults.yaml")

    samples = {}
    for name, data in samples_raw.items():
        source = data["source"]
        if source.startswith("s3://"):
            raise ValueError(
                f"sample {name!r} `source` must be a bucket-relative key prefix "
                f"(e.g. `data/wgs/HG00096/`), not a full S3 URI; got {source!r}. "
                f"The S3 bucket comes from defaults.yaml or BWA_MEM3_BENCH_S3_BUCKET."
            )
        samples[name] = Sample(
            name=name,
            baseline_tool=data["baseline_tool"],
            reference=data["reference"],
            source=source,
            layout=data.get("layout", "paired"),
            fg_labs_flags=_as_str_list(
                f"sample {name!r}", "fg_labs_flags", data.get("fg_labs_flags", [])
            ),
            mem_flags=_as_str_list(f"sample {name!r}", "mem_flags", data.get("mem_flags", [])),
            compare_options=_validate_compare_options(name, data.get("compare_options")),
            truth=_as_bool(name, "truth", data.get("truth", False)),
        )

    archs = {
        name: Arch(
            name=name,
            instance_type=data["instance_type"],
            batch_queue=data["batch_queue"],
            simd=data["simd"],
            platform=data["platform"],
            baseline_arch=str(data.get("baseline_arch", "")),
        )
        for name, data in archs_raw["archs"].items()
    }

    return WorkflowConfig(
        samples=samples,
        archs=archs,
        core_arch=archs_raw["core_arch"],
        full_archs=list(archs_raw["full_archs"]),
        region=defaults["region"],
        bucket=defaults["bucket"],
        ecr_repo=defaults["ecr_repo"],
        upstream_tag=defaults["upstream_tag"],
        bwameth_version=defaults["bwameth_version"],
        bwa_version=defaults["bwa_version"],
        threads=int(defaults["threads"]),
        reps_default=int(defaults["reps_default"]),
        reps_baseline=int(defaults["reps_baseline"]),
        thread_scaling=_thread_scaling_from(
            defaults["thread_scaling"], samples=samples, archs=archs
        ),
        references=defaults["references"],
        runs_prefix=defaults["runs_prefix"],
        baseline_prefix=defaults["baseline_prefix"],
        golden_prefix=defaults["golden_prefix"],
        data_prefix=defaults["data_prefix"],
        compare_defaults=compare_defaults,
    )
