"""Load samples + archs + defaults from YAML into typed records."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# A SAM aux tag name: exactly two characters, `[A-Za-z][A-Za-z0-9]` (SAMv1 §1.5).
_AUX_TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9]")

# The `compare-bams` invocations the workflow makes, one per rule in
# `workflow/rules/compare.smk`. Three flavours, not two:
#   - `vs_baseline` — a DIFFERENT aligner (upstream bwa-mem2, or bwameth for
#     meth samples), so it skips the tags the two never share.
#   - `vs_golden` / `vs_x86` — same binary AND same search settings, so every
#     tag is comparable and nothing is skipped.
#   - `vs_default` — the exception to the "same binary, skip nothing" rule:
#     `--fast` prunes the candidate set on purpose, so the tags describing that
#     set are skipped while the tags describing the chosen alignment stay strict.
COMPARE_KINDS = frozenset({"vs_baseline", "vs_golden", "vs_x86", "vs_default", "vs_bwa"})

# For the two CROSS-TOOL kinds, the `--compat` target whose output shaping makes
# bwa-mem3 tag-symmetric with the other side. `WorkflowConfig.ignore_tags()`
# drops the kind's default exclusions only for a sample whose target matches --
# a `--compat=bwa-mem` arm scored against the bwa-mem2 baseline retains MQ:i and
# is NOT symmetric, so it must keep them. The same-binary kinds (vs_golden,
# vs_x86, vs_default) are absent on purpose: there is no upstream, and they are
# already strict for every sample.
COMPARE_KIND_UPSTREAM = {
    "vs_baseline": "bwa-mem2",
    "vs_bwa": "bwa-mem",
}

# The per-kind tag lists a `compare_options` / `compare_defaults` body may carry.
# Enumerated once so adding a third list is one edit, not three.
_TAG_LIST_KEYS = ("ignore_tags", "expect_tags")

# Mate tags. A single-end read has no mate, so these cannot exist on one -- a
# logical impossibility, not an aligner choice. Measured: `sbx-1M` carries
# neither on either side of any comparison.
MATE_ONLY_TAGS = frozenset({"MQ", "MC"})

# Tags neither side emits under `--meth`, ON A BUILD PREDATING
# fg-labs/bwa-mem3#304. `bwa-mem3`'s methylation output goes through a separate
# writer (`src/meth_bam.cpp`) which, until that PR, omitted MQ and HN; bwameth
# emits neither either. Measured: 0 of 10,369,692 primaries on
# `meth-twist-emseq-5M`, likewise on `smoke-meth`.
#
# Unlike MATE_ONLY_TAGS this was a defect, not a law -- fg-labs/bwa-mem3#296,
# fixed by #304. The exemption still has to stand while any pre-#304 SHA is
# benched (an old golden re-run, a bisect), because on those builds the two
# `vs_baseline` ignore entries genuinely match no record. On a post-#304 build it
# is simply redundant, not wrong: bwa-mem3 emits both, bwameth still does not, so
# the entries are live and the audit would have passed anyway.
#
# DELETE THIS CONSTANT once no pre-#304 build is in rotation. Note it only
# exempts the two tags from the dead-entry audit -- they stay on `ignore_tags`,
# because bwameth will never emit them.
METH_UNEMITTED_TAGS = frozenset({"MQ", "HN"})

# Name suffixes marking a `--compat` sibling, mapped to the target each one
# declares. The baseline alias in `workflow/rules/compare.smk` strips the suffix
# to find the base sample whose upstream BAM the sibling is scored against, so
# these are load-bearing, not cosmetic.
#
# `-compat` is grandfathered as bwa-mem2 rather than renamed to
# `-compat-bwa-mem2`: sample names are S3 cache keys, so a rename would orphan
# every blessed compat BAM and force a re-run of all eight arms across every
# arch to buy nothing but symmetry.
#
# The mapping is checked, not just used: a sibling whose `--compat=` target
# disagrees with its own suffix fails config load. Otherwise a `-compat` arm
# silently switched to `--compat=bwa-mem` would keep being scored against the
# bwa-mem2 baseline, where its retained MQ:i reads as a compat regression.
COMPAT_SAMPLE_SUFFIXES = {
    "-compat-bwa-mem": "bwa-mem",
    "-compat": "bwa-mem2",
}


def compat_sample_suffix(sample_name: str) -> str | None:
    """The `COMPAT_SAMPLE_SUFFIXES` key `sample_name` ends with, or None.

    Longest match wins. No two current suffixes are suffixes of each other
    (`"…-compat-bwa-mem".endswith("-compat")` is False), so the ordering is
    belt-and-braces against a future key like `-compat-bwa-mem-0.7.17` that
    would otherwise be shadowed.

    :param sample_name: sample name to classify.
    :return: the matching suffix, or None if the name declares no compat target.
    """
    for suffix in sorted(COMPAT_SAMPLE_SUFFIXES, key=len, reverse=True):
        if sample_name.endswith(suffix):
            return suffix
    return None


# Tags that appear only on methylation comparisons: XM/XG/XR from `bwa-mem3
# --meth`, and YD/YC/RG from bwameth. Derived rather than declared per sample
# because it is one fact about bisulfite alignment, and restating it across ~10
# meth samples x 3 comparison kinds invites exactly the drift this guard exists
# to catch.
METH_EXTRA_TAGS = frozenset({"XM", "XG", "XR", "YD", "YC", "RG"})

# Tags that appear only under ALT-aware alignment. `pa:f:` is the ratio of a
# region's score to that of the ALT region shadowing it, emitted by bwa
# (bwamem.c), bwa-mem2 (bwamem.cpp) and bwa-mem3 alike off the same
# `score/alt_sc` and untouched by `--compat` -- `compat_target_t` shapes only
# @HD, the sidecar, MQ and HN.
#
# Conditional on the sidecar, not merely rare: `alt_sc` is set for a NON-ALT
# region whose better overlapping hit is on an ALT contig, and with no `.alt`
# staged nothing is ever marked ALT, so the tag cannot be emitted at all. That
# is why 323M records of prior compat testing never saw one.
#
# Allowlisted (`expect_tags`) and never ignored. All three aligners compute it
# identically, so a difference is a real finding -- and scoping it to ALT
# samples keeps a `pa` on a non-ALT cell, which would mean a sidecar leaked into
# a run that should not have one, failing the guard by name.
#
# Derived from `alt_aware` rather than declared per sample, for the same reason
# METH_EXTRA_TAGS is derived from `is_meth`: several samples x several kinds is
# that many places for one fact to drift.
ALT_EXTRA_TAGS = frozenset({"pa"})

# Tags not comparable between `bwa-mem3 --meth` and bwameth, excluded from the
# score on the `vs_baseline` kind only (the other three are meth-vs-meth, where
# every tag is comparable).
#
# NM/MD are edit distances against a C->T/G->A converted reference; XA
# (`rname,pos,CIGAR,NM`) and SA (`rname,pos,strand,CIGAR,mapQ,NM`) embed that
# edit distance AND doubled-reference contig names (`fchr1`). XM/XG/XR and
# YD/YC/RG are the two tools' disjoint bisulfite tag sets. Measured: each
# diverges on >99.5% of reads, and excluding them leaves +0.14pp of added drift.
#
# Derived rather than declared for the same reason as METH_EXTRA_TAGS, and the
# two must stay derived TOGETHER. When only the allowlist half was derived, the
# ignore half sat copy-pasted on 3 of 12 meth samples and absent from the other
# 9 -- so promoting a `sim-meth-*` sample into SWEEP_SAMPLES would have sent
# NM/MD/XA/SA strict against bwameth (a ~100% crater) while the guard stayed
# silent, because METH_EXTRA_TAGS had already allowlisted every tag involved.
METH_IGNORE_TAGS = frozenset({"NM", "MD", "XA", "SA", "XM", "XG", "XR", "YD", "YC", "RG"})


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
    # `COMPARE_KINDS`). Each kind's body accepts two tag lists:
    #     {"vs_baseline": {"ignore_tags": [...], "expect_tags": [...]}}
    # `ignore_tags` excludes a tag from the score; `expect_tags` declares a tag
    # MAY appear, so the guard does not flag it as unexpected. BOTH EXTEND the
    # matching `compare_defaults` entry rather than replacing it. Resolve via
    # `WorkflowConfig.ignore_tags()` / `.expect_tags()` — never read this
    # directly, or the defaults get silently dropped.
    compare_options: dict[str, Any] = field(default_factory=dict)
    # Truth-based accuracy sample (holodeck-simulated). When True, the sample's
    # S3 `source` prefix also holds the truth artifacts (`golden.bam`,
    # `truth.vcf`, and for meth samples `cpg-truth.bedGraph`) that the
    # `eval_accuracy` rule grades the aligner BAM against. Truth samples are
    # driven by the `accuracy` / `accuracy_smoke` targets, NOT the speed/
    # concordance sweep (`rule all` / `baseline_all` exclude them).
    truth: bool = False
    # Stage the `<prefix>.alt` sidecar alongside the index, turning on ALT-aware
    # mapping in every aligner that reads it (bwa, bwa-mem2, bwa-mem3 alike).
    #
    # Off by default, and the default is load-bearing. The sidecar lives in the
    # SAME S3 prefix as the index it belongs to, so this flag -- not the
    # reference name -- is what decides whether a run sees it. That keeps the
    # existing blessed corpus valid: a sample without this flag stages exactly
    # the files it always did and produces exactly the output it always did,
    # with no second 21 GB reference tree to hold a 487 KB file.
    #
    # What it turns on is not marginal. `bns_restore` marks the first field of
    # every non-`@` line in the sidecar as ALT, and for hg38 that is 3,171 of
    # 3,366 contigs (261 `_alt` + 525 HLA + 2,385 `_decoy` -- decoys are listed
    # as FLAG-4 unmapped records on purpose, which is how bwakit ships it). So
    # an alt-aware run exercises ALT-specific primary selection, the ALT MAPQ
    # adjustment, the `pa:f:` tag and the `-h INT,INT` alt hit cap -- an entire
    # code path that is otherwise never executed by this harness.
    alt_aware: bool = False

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
        # ALT-awareness is a property of the 4-letter index, and the bisulfite
        # branches of `_ref_inputs` return before the `.alt` append -- they stage
        # a different index family entirely (`.bwameth.c2t` for bwameth, `.meth.*`
        # for `bwa-mem3 --meth`), under a different reference prefix that holds no
        # sidecar. So a meth sample carrying this flag would stage nothing extra,
        # run exactly as alt-naive as it always did, and report a clean result
        # while its config claimed an ALT-aware run. Reject at load rather than
        # let a green cell prove nothing.
        if self.alt_aware and self.is_meth:
            raise ValueError(
                f"sample {self.name!r} combines alt_aware with methylation "
                f"(baseline_tool={self.baseline_tool!r}, fg_labs_flags={self.fg_labs_flags}). "
                f"The bisulfite index families carry no `.alt` sidecar, so the run would be "
                f"silently ALT-naive."
            )
        # Mirror the two guards `bwa-mem3 mem` enforces at runtime, so an
        # impossible sample fails at config load instead of on a Batch worker
        # twenty minutes into a bless. `--compat` is output-shaping only, and
        # both of these change the alignments themselves:
        #   [E::main_mem] --compat and --fast are mutually exclusive
        #   [E::main_mem] --compat is not supported with --meth
        if self.is_compat:
            if self.is_meth:
                raise ValueError(
                    f"sample {self.name!r} combines --compat with methylation "
                    f"(baseline_tool={self.baseline_tool!r}, fg_labs_flags={self.fg_labs_flags}). "
                    f"bwa-mem2 has no bisulfite mode, so bwa-mem3 rejects the combination."
                )
            if "--fast" in self.fg_labs_flags:
                raise ValueError(
                    f"sample {self.name!r} combines --compat with --fast "
                    f"(fg_labs_flags={self.fg_labs_flags}). --fast changes alignments while "
                    f"--compat only shapes output, so the pair would produce a diff-clean-"
                    f"looking stream over genuinely different alignments."
                )

    @property
    def compat_target(self) -> str:
        """The `--compat` target this sample requests, or `""` for none.

        Only the `--compat=<target>` spelling is recognised: the workflow builds
        flag lists programmatically, so the space-separated form never occurs.
        """
        for flag in self.fg_labs_flags:
            if flag.startswith("--compat="):
                return flag.split("=", 1)[1]
        return ""

    @property
    def is_compat(self) -> bool:
        """Whether this sample asks bwa-mem3 to reproduce another aligner's
        output byte-for-byte.

        `--compat=off` selects native output and is explicitly NOT a compat
        sample -- it is pointer-identical to passing no flag at all, so treating
        it as one would apply the strict tag policy to a default-mode arm.
        """
        return self.compat_target not in ("", "off")

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


# Seconds per host-contention probe when `thread_scaling.host_probe_seconds` is
# absent. tachyon's own default, and 20 s of probing either side of a ~45-minute
# ladder is ~0.7% overhead — cheap enough that the reading is never worth
# skipping, long enough that a transient neighbour does not dominate the sample.
_DEFAULT_HOST_PROBE_SECONDS = 10.0

# Seconds per host-contention probe for the regular per-cell sweep
# (`align_fg_labs`), when `sweep_host_probe_seconds` is absent. Deliberately far
# below tachyon's own 10 s default, which is sized for the ladder's ~45-minute
# job: a per-cell pre+post pair runs on EVERY sweep cell, including smoke-1M's
# ~2 s wall time, where a 10 s-per-side pair would be a 10x overhead rather than
# a rounding error. 2 s per side is a few percent of a typical 20-130 s real-data
# cell and roughly matches smoke-1M's own wall time -- short enough that probing
# never dominates, long enough that tachyon's own guidance (a probe needs to run
# past its warm-up to be a reading rather than noise) is still met.
_DEFAULT_SWEEP_HOST_PROBE_SECONDS = 2.0


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
    # Wall-clock budget for each of the two tachyon host-contention probes that
    # bracket the ladder. Defaulted rather than required: it is a diagnostic knob,
    # and a config that omits it should still load.
    host_probe_seconds: float = _DEFAULT_HOST_PROBE_SECONDS

    @property
    def max_threads(self) -> int:
        """Largest thread count in the ladder — what the job must reserve."""
        return max(step.threads for step in self.ladder)


@dataclass(frozen=True)
class Arena:
    """Configuration for the release-history "arena" comparison (`--target arena`).

    Interleaves every blessed bwa-mem3 release (baked into the builder base
    image, see `docker/Dockerfile.base`) plus lh3/bwa, upstream bwa-mem2, and
    minibwa on ONE fixed host per arch, so a release-over-release wall-time
    claim never has to trust two builds measured weeks apart on different
    machines -- see `workflow/rules/arena.smk` for the full rationale.

    Unlike `ThreadScaling`, the arm list itself is NOT config-driven: it is
    the fixed set of binaries the base image bakes in, hardcoded in
    `arena.smk`. Only the measurement scope (which sample, which archs, how
    many reps) lives here.
    """

    sample: str
    archs: list[str]
    reps: int
    threads: int
    host_probe_seconds: float = _DEFAULT_HOST_PROBE_SECONDS


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
    # `-K` in bases, passed to BOTH bwa-mem3 and upstream bwa-mem2 so their output
    # is thread-invariant and the golden does not depend on `threads`. See the long
    # rationale in config/defaults.yaml; the short version is that the default batch
    # is `chunk_size * n_threads` and mem_pestat reads it, so unpinned output is a
    # function of -t. Deliberately NOT derived from `threads`.
    batch_bases: int
    thread_scaling: ThreadScaling
    arena: Arena
    references: dict[str, dict[str, str]]
    runs_prefix: str
    baseline_prefix: str
    golden_prefix: str
    data_prefix: str
    # Per-comparison-kind defaults, keyed by kind, each a mapping with
    # `ignore_tags` and `expect_tags` lists. Per-sample `compare_options` extend
    # these.
    compare_defaults: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # Wall-clock budget for each of the two tachyon host-contention probes that
    # bracket every regular sweep cell (`align_fg_labs`). Defaulted, like
    # `thread_scaling.host_probe_seconds`: a diagnostic knob, not a decision a
    # config must make.
    sweep_host_probe_seconds: float = _DEFAULT_SWEEP_HOST_PROBE_SECONDS

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

        Methylation samples get `METH_IGNORE_TAGS` added automatically on
        `vs_baseline`. That is the cross-tool kind where the two sides can be
        *different kinds of aligner* (bwameth is 3-letter, bwa-mem3 is not); the
        other cross-tool kind, `vs_bwa`, has no meth arm because `--compat` and
        `--meth` are mutually exclusive, so it needs no such exemption. See that
        constant for why it is derived rather than declared, and why it must
        stay derived in lockstep with `METH_EXTRA_TAGS` in `expect_tags`.

        `vs_golden` carries no exemptions at all, for meth or anything else: two
        fg-labs builds should be ~identical, so Gate #2 is strict on every tag.

        A `--compat` sample inverts the usual direction: it SUBTRACTS the
        default exclusions rather than adding to them, so the comparison is
        strict on every tag. The default list exists because default-mode
        bwa-mem3 emits MQ/HN and upstream does not; a compat target suppresses
        exactly what its own upstream lacks, so both sides end up with the same
        tag set and excluding anything would hide the regression the arm exists
        to catch -- a build that stopped suppressing would still score 100%.
        Strictness is the whole assertion: byte-identity, not
        identity-modulo-the-tags-we-excused.

        **The subtraction applies only when the target matches the aligner on
        the other side**, which is why it consults `COMPARE_KIND_UPSTREAM`
        rather than just `is_compat`. A `--compat=bwa-mem` arm scored against
        the bwa-mem2 baseline is NOT tag-symmetric: it retains `MQ:i` (bwa emits
        it; bwa-mem2 forked before that landed), so a strict list would score
        every mated record as discordant and read as a compat regression that
        never happened. That arm falls through to the kind default, which
        excludes MQ and HN -- correct on both counts, since HN is absent from
        both sides there and excluding an absent tag is a no-op.

        This is why the subtraction lives here rather than in the YAML:
        `_resolve_tags` unions a sample's list onto the kind default and cannot
        express a removal.

        :param sample_name: sample being compared.
        :param kind: comparison kind, one of `COMPARE_KINDS`.
        :return: sorted, de-duplicated tag names.
        """
        sample = self.samples[sample_name]
        if sample.is_compat and sample.compat_target == COMPARE_KIND_UPSTREAM.get(kind):
            return []
        tags = self._resolve_tags(sample_name, kind, "ignore_tags")
        if sample.is_meth and kind == "vs_baseline":
            tags |= METH_IGNORE_TAGS
        return sorted(tags)

    def expect_tags(self, sample_name: str, kind: str) -> list[str]:
        """Aux tags that MAY appear for one (sample, comparison kind).

        Any tag `compare-bams` observes that is on neither this list nor
        `ignore_tags` fails the run by name. The semantics are *may* appear, not
        *must*: a listed tag that never shows up is a harmless no-op, which is
        what lets one per-kind list serve samples whose tag sets legitimately
        differ without needing a per-sample subtraction.

        Methylation samples get `METH_EXTRA_TAGS` added automatically, and
        ALT-aware samples `ALT_EXTRA_TAGS` -- see those constants for why both
        are derived rather than declared.

        :param sample_name: sample being compared.
        :param kind: comparison kind, one of `COMPARE_KINDS`.
        :return: sorted, de-duplicated tag names.
        """
        tags = self._resolve_tags(sample_name, kind, "expect_tags")
        sample = self.samples[sample_name]
        if sample.is_meth:
            tags |= METH_EXTRA_TAGS
        if sample.alt_aware:
            tags |= ALT_EXTRA_TAGS
        return sorted(tags)

    def absent_ok_tags(self, sample_name: str, kind: str) -> list[str]:
        """`ignore_tags` entries known to be absent for one (sample, kind).

        These are exempt from `compare-bams`' dead-entry check, which otherwise
        fails a run whose `ignore_tags` names a tag matching no record. Two
        populations qualify: mate tags on single-end samples (impossible by
        definition) and MQ/HN on methylation samples (absent by defect on any
        build predating fg-labs/bwa-mem3#304, which closed #296).

        The result is intersected with `ignore_tags()` -- the DERIVED list, not
        the raw config -- because only ignore entries are ever audited; naming a
        tag that is not ignored would be inert config, which is the very thing
        this guard exists to reject (`TagGuardViolation::RedundantAbsentOk`).

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
        return sorted(absent.intersection(self.ignore_tags(sample_name, kind)))


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


def _as_tag_list(owner: str, key: str, value: Any) -> list[str]:
    """Validate a tag list is a `list[str]` of well-formed SAM aux tag names.

    A SAM aux tag is exactly two characters, `[A-Za-z][A-Za-z0-9]`. Anything else
    can never match a tag `compare-bams` reads off a record, so a typo like
    `NMX` would sit in the config doing nothing.

    That matters asymmetrically. A typo in `ignore_tags` IS caught at run time --
    the tag matches no record, so the dead-entry check fires. A typo in
    `expect_tags` is not: the list means "may appear", so an entry that can never
    appear is indistinguishable from one that legitimately does not. Validating
    the shape here is what keeps the guard's own config subject to the rule the
    guard exists to enforce -- config that silently does nothing is the bug.

    :param owner: what the list belongs to, rendered verbatim into the error
        message (e.g. `"sample 'wgs-5M'"` or ``"`compare_defaults`"``). The
        top-level block uses this helper too, so it is not always a sample.
    :param key: config key being validated (e.g. `compare_defaults.vs_x86`).
    :param value: raw value read from YAML.
    :return: the value as a `list[str]`.
    :raises ValueError: if `value` is not a list of strings, or any entry is not
        a well-formed two-character aux tag name.
    """
    tags = _as_str_list(owner, key, value)
    malformed = [t for t in tags if not _AUX_TAG_RE.fullmatch(t)]
    if malformed:
        raise ValueError(
            f"{owner} `{key}` has malformed aux tag name(s) "
            f"{malformed}. A SAM aux tag is exactly two characters matching "
            f"[A-Za-z][A-Za-z0-9] (e.g. NM, MD, XS). An entry of any other shape "
            f"can never match a tag on a record, so it would sit in the config "
            f"doing nothing."
        )
    return tags


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


def _reject_unknown_keys(owner: str, where: str, body: dict[str, Any]) -> None:
    """Reject keys nothing reads inside a comparison-kind body.

    Only `_TAG_LIST_KEYS` are consulted, so a near-miss like `expect_tag`
    (singular) would load clean and configure nothing -- inert config, which is
    the exact failure mode the tag guard exists to reject. Catch it at load.

    :param owner: what the body belongs to, for the error message.
    :param where: the config path being validated (e.g. `compare_options.vs_x86`).
    :param body: the kind body read from YAML.
    :raises ValueError: if `body` carries any key outside `_TAG_LIST_KEYS`.
    """
    unknown = sorted(set(body) - set(_TAG_LIST_KEYS))
    if unknown:
        raise ValueError(
            f"{owner} `{where}` has unknown key(s) {unknown}; expected one of "
            f"{sorted(_TAG_LIST_KEYS)}. A key nothing reads would sit in the "
            f"config doing nothing."
        )


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
        _reject_unknown_keys(f"sample {sample_name!r}", f"compare_options.{key}", body)
        for list_key in _TAG_LIST_KEYS:
            _as_tag_list(
                f"sample {sample_name!r}",
                f"compare_options.{key}.{list_key}",
                body.get(list_key, []),
            )
    return options


def _validate_compat_siblings(samples: dict[str, Sample]) -> None:
    """Every `--compat` sibling must have a base sample it agrees with.

    `compare_vs_baseline` aliases a `<base>-compat` sample onto `<base>`'s
    baseline BAM so upstream bwa-mem2 is not run twice over identical input
    (`_baseline_sample` in `workflow/rules/compare.smk`). That aliasing is only
    correct while the two agree on every input the baseline alignment consumes.
    If a sibling's `source` were edited and its base's were not, the compat arm
    would silently score one dataset's reads against another dataset's BAM --
    near-total discordance attributed to a compat regression that never happened.

    Checked here rather than in `Sample.__post_init__` because it is a relation
    between two samples, which a single sample cannot see.

    :param samples: every configured sample, keyed by name.
    :raises ValueError: if a compat sibling has no base, or disagrees with it on
        `baseline_tool`, `reference`, `source`, `layout`, or `mem_flags`.
    """
    # `alt_aware` belongs here for the same reason the others do: it changes the
    # ALIGNMENT (3,171 hg38 contigs become ALT), so a sibling that enabled it
    # while its base did not would be scored against a baseline computed with
    # ALT-awareness OFF -- a guaranteed, and entirely spurious, compat failure.
    baseline_inputs = (
        "baseline_tool",
        "reference",
        "source",
        "layout",
        "mem_flags",
        "alt_aware",
    )
    for name, sample in sorted(samples.items()):
        if not sample.is_compat:
            continue
        suffix = compat_sample_suffix(name)
        if suffix is None:
            expected = "', '<base>".join(sorted(COMPAT_SAMPLE_SUFFIXES))
            raise ValueError(
                f"sample {name!r} sets --compat but is not named '<base>{expected}'. "
                f"The baseline alias strips that suffix to find the base sample, so a "
                f"differently-named compat sample would be scored against its own "
                f"(redundantly realigned) baseline."
            )
        want_target = COMPAT_SAMPLE_SUFFIXES[suffix]
        if sample.compat_target != want_target:
            raise ValueError(
                f"compat sample {name!r} is named '{suffix}' but requests "
                f"--compat={sample.compat_target}. The suffix decides which upstream the "
                f"arm is scored against, so a mismatch would grade the output against the "
                f"wrong aligner -- and the two targets disagree on MQ:i and @HD by design, "
                f"so it would read as a compat regression rather than a naming error."
            )
        base_name = name[: -len(suffix)]
        base = samples.get(base_name)
        if base is None:
            raise ValueError(
                f"compat sample {name!r} has no base sample {base_name!r}. "
                f"The compat arm is scored against the base sample's baseline BAM, "
                f"which cannot exist if the base is not configured."
            )
        mismatched = [
            field_name
            for field_name in baseline_inputs
            if getattr(sample, field_name) != getattr(base, field_name)
        ]
        if mismatched:
            raise ValueError(
                f"compat sample {name!r} disagrees with its base {base_name!r} on "
                f"{', '.join(mismatched)}. Both are aligned by upstream bwa-mem2 from the "
                f"same baseline BAM, so every input to that alignment must match."
            )


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
        kind: {k: [] for k in _TAG_LIST_KEYS} for kind in COMPARE_KINDS
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
        _reject_unknown_keys("`compare_defaults`", kind, body)
        out[kind] = {
            list_key: _as_tag_list(
                "`compare_defaults`", f"{kind}.{list_key}", body.get(list_key, [])
            )
            for list_key in _TAG_LIST_KEYS
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

    # Optional, unlike the keys above: a diagnostic probe's duration is not a
    # decision a config must make. Validated with the same rigour anyway — it is
    # pasted into the ladder's shell body, so a malformed value would surface as a
    # failed Batch job rather than a load error. `nan`/`inf` are rejected for the
    # concrete reason that both reach `emit-host-probe` as a literal argument its
    # numeric guard rejects, failing the ladder over a diagnostic.
    raw_probe = raw.get("host_probe_seconds", _DEFAULT_HOST_PROBE_SECONDS)
    if (
        isinstance(raw_probe, bool)
        or not isinstance(raw_probe, (int, float))
        or not math.isfinite(raw_probe)
        or raw_probe <= 0
    ):
        raise ValueError(
            f"`thread_scaling.host_probe_seconds` must be a finite number > 0; got {raw_probe!r}"
        )

    return ThreadScaling(
        sample=sample,
        arch=arch,
        ladder=sorted(ladder, key=lambda s: s.threads),
        max_efficiency_drop_pp=drop,
        host_probe_seconds=float(raw_probe),
    )


def _arena_from(raw: Any, *, samples: dict[str, Sample], archs: dict[str, Arch]) -> Arena:
    """Validate and build the `arena` block from `defaults.yaml`.

    Fails loudly at load time, same rationale as `_thread_scaling_from`: the
    arena drives an on-demand Batch job per arch, so a typo here should not
    surface an hour into a paid run.

    :param raw: the `arena` mapping read from YAML.
    :param samples: parsed samples, to check the referenced sample exists.
    :param archs: parsed archs, to check every referenced arch exists.
    :return: the validated `Arena`.
    :raises ValueError: on a missing key, unknown sample/arch, an empty
        `archs` list, or a `reps`/`threads` value that is not an integer >= 1.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"`arena` must be a mapping; got {raw!r}")
    for key in ("sample", "archs", "reps", "threads"):
        if key not in raw:
            raise ValueError(f"`arena` is missing required key {key!r}")

    sample = raw["sample"]
    if sample not in samples:
        raise ValueError(f"`arena.sample` {sample!r} is not a configured sample")

    raw_archs = raw["archs"]
    if not isinstance(raw_archs, list) or not raw_archs:
        raise ValueError(f"`arena.archs` must be a non-empty list; got {raw_archs!r}")
    for arch in raw_archs:
        if arch not in archs:
            raise ValueError(f"`arena.archs` entry {arch!r} is not a configured arch")

    return Arena(
        sample=sample,
        archs=list(raw_archs),
        reps=_as_positive_int("`arena`", "reps", raw["reps"]),
        threads=_as_positive_int("`arena`", "threads", raw["threads"]),
        host_probe_seconds=float(raw.get("host_probe_seconds", _DEFAULT_HOST_PROBE_SECONDS)),
    )


def _sweep_host_probe_seconds_from(defaults: dict[str, Any]) -> float:
    """Validate the top-level `sweep_host_probe_seconds` default.

    Same rigour as `thread_scaling.host_probe_seconds` and for the same
    concrete reason: it is pasted into `align_fg_labs`'s shell body, so a
    malformed value fails a sweep cell rather than the config load.
    """
    raw_probe = defaults.get("sweep_host_probe_seconds", _DEFAULT_SWEEP_HOST_PROBE_SECONDS)
    if (
        isinstance(raw_probe, bool)
        or not isinstance(raw_probe, (int, float))
        or not math.isfinite(raw_probe)
        or raw_probe <= 0
    ):
        raise ValueError(
            f"`sweep_host_probe_seconds` must be a finite number > 0; got {raw_probe!r}"
        )
    return float(raw_probe)


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
            alt_aware=_as_bool(name, "alt_aware", data.get("alt_aware", False)),
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

    _validate_compat_siblings(samples)

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
        batch_bases=_as_positive_int("defaults.yaml", "batch_bases", defaults["batch_bases"]),
        thread_scaling=_thread_scaling_from(
            defaults["thread_scaling"], samples=samples, archs=archs
        ),
        arena=_arena_from(defaults["arena"], samples=samples, archs=archs),
        references=defaults["references"],
        runs_prefix=defaults["runs_prefix"],
        baseline_prefix=defaults["baseline_prefix"],
        golden_prefix=defaults["golden_prefix"],
        data_prefix=defaults["data_prefix"],
        compare_defaults=compare_defaults,
        sweep_host_probe_seconds=_sweep_host_probe_seconds_from(defaults),
    )
