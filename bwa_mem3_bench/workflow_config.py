"""Load samples + archs + defaults from YAML into typed records."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


def _as_str_list(sample_name: str, key: str, value: Any) -> list[str]:
    """Validate a YAML flag value is a ``list[str]`` before coercion.

    ``list(...)`` on a bare YAML scalar (e.g. ``mem_flags: -5``) silently
    splits the string into characters (``['-', '5']``), corrupting the
    alignment arguments. Reject anything that is not already a list of
    strings so a misconfiguration fails loudly at load time.

    :param sample_name: sample the flags belong to (for the error message).
    :param key: config key being validated (e.g. ``"mem_flags"``).
    :param value: raw value read from YAML.
    :return: the value as a ``list[str]``.
    :raises ValueError: if ``value`` is not a list of strings.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"sample {sample_name!r} `{key}` must be a list of strings; got {value!r}")
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
        a ladder without a 1-thread rung, duplicate thread counts, or a
        non-positive threads/reps value.
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
        threads, reps = int(entry["threads"]), int(entry["reps"])
        if threads < 1 or reps < 1:
            raise ValueError(
                f"`thread_scaling.ladder` entry {entry!r} must have threads >= 1 and reps >= 1"
            )
        ladder.append(ThreadScalingStep(threads=threads, reps=reps))

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

    drop = float(raw["max_efficiency_drop_pp"])
    if drop < 0:
        raise ValueError(f"`thread_scaling.max_efficiency_drop_pp` must be >= 0; got {drop}")

    return ThreadScaling(
        sample=sample,
        arch=arch,
        ladder=sorted(ladder, key=lambda s: s.threads),
        max_efficiency_drop_pp=drop,
    )


def load_config(config_dir: Path) -> WorkflowConfig:
    """Load and validate the three-file config into a `WorkflowConfig`."""
    samples_raw = _read_yaml(config_dir / "samples.yaml")["samples"]
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
            fg_labs_flags=_as_str_list(name, "fg_labs_flags", data.get("fg_labs_flags", [])),
            mem_flags=_as_str_list(name, "mem_flags", data.get("mem_flags", [])),
            compare_options=dict(data.get("compare_options", {})),
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
    )
