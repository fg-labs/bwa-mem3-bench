"""Build the per-release results snapshot (``data.json``) from ``benchmark.db``.

The published ``results/`` pages are rendered from this snapshot, never straight
from the database: ``benchmark.db`` is local and gitignored, so committing the
snapshot next to the pages is what lets anyone -- and the test suite --
regenerate them and check they are current (see :mod:`bwa_mem3_bench.report.results`).

The snapshot holds aggregates only. Per-rep rows, instance IDs, availability
zones, spot prices and S3 locations stay in the database.

Every aggregate is a MEDIAN across reps, and every speed ratio is oriented so
that ``> 1`` means bwa-mem3 is faster.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bwa_mem3_bench import minibwa_sha
from bwa_mem3_bench.arena_ladder import arena_releases
from bwa_mem3_bench.release_allowances import ReleaseAllowance, sha_prefix_match
from bwa_mem3_bench.storage.ingest import baseline_sha_for
from bwa_mem3_bench.storage.queries import query_df
from bwa_mem3_bench.workflow_config import WorkflowConfig

SNAPSHOT_SCHEMA_VERSION = 1

# The comparison kinds, as stored in `comparisons.kind`.
_VS_BASELINE = "vs-baseline"
_VS_GOLDEN = "vs-golden"
_VS_X86 = "vs-x86"
_VS_DEFAULT = "vs-default"

# Sibling arms of a dataset, keyed by the suffix they carry on the sample name.
_ARM_SUFFIXES = {"default": "", "fast": "-fast", "compat": "-compat"}

# MAPQ bins at or above this lower bound count as "confident" in the accuracy
# summary -- the same MAPQ >= 20 cut the meth confident-placement gate uses.
_CONFIDENT_MAPQ = 20

# Arena labels for the three non-bwa-mem3 comparators.
_COMPARATOR_LABELS = ("bwa", "bwa-mem2-upstream", "minibwa")

# Arena labels for the candidate being published, in each mode.
_CANDIDATE_LABELS = {"default": "fg-labs-default", "fast": "fg-labs-fast"}

# Arena ladder labels for 0.x releases: the minor digits then one patch digit,
# e.g. `v021` (v0.2.1) and `v110` (v0.11.0).
_RELEASE_LABEL = re.compile(r"^v(\d+)(\d)$")


@dataclass(frozen=True)
class Dataset:
    """A published dataset: its sample name plus the public description of it."""

    sample: str
    title: str
    source: str
    notes: str = ""


# The datasets that get a page, in display order. Smoke samples and the
# simulated truth sets (which get the accuracy page instead) are deliberately
# absent. The source text is public provenance; keep it in step with
# docs/data-setup.md.
DATASETS: tuple[Dataset, ...] = (
    Dataset(
        sample="wgs-5M",
        title="Whole genome (HG00096)",
        source="1000 Genomes HG00096 30x WGS (NYGC, GRCh38), downsampled to 5M read pairs.",
    ),
    Dataset(
        sample="wes-5M",
        title="Whole exome (HG00100)",
        source="1000 Genomes HG00100 phase-3 Illumina exome, downsampled to 5M read pairs.",
    ),
    Dataset(
        sample="panel-agilent-qxt-5M",
        title="Targeted panel (Agilent SureSelect QXT)",
        source=(
            "Agilent SureSelect QXT hereditary-cancer panel, SRR15497869 (PRJNA755485), "
            "downsampled to 5M read pairs."
        ),
        notes=(
            "Deep target coverage produces many split alignments, so this dataset leans on "
            "supplementary-alignment handling more than the others."
        ),
    ),
    Dataset(
        sample="hic-1M",
        title="Hi-C (HG002)",
        source=(
            "HG002 Hi-C, 1M read pairs (2x151 bp) from Zenodo 10.5281/zenodo.19703025 (CC BY 4.0)."
        ),
        notes=(
            "Aligned with `-5SP` on both bwa-mem3 and bwa-mem2, the canonical Hi-C settings "
            "(no mate rescue, no pairing, smallest-coordinate split as primary)."
        ),
    ),
    Dataset(
        sample="sbx-1M",
        title="Roche SBX (HG002, single-end)",
        source=(
            "About 1M single-end Roche SBX reads, 50-974 bp (median ~224 bp), HG002, from the "
            "Roche Axelios GIAB demonstration data (CC BY-NC 4.0)."
        ),
    ),
    Dataset(
        sample="meth-twist-emseq-5M",
        title="Methylation (Twist EM-seq)",
        source="Twist EM-seq library, downsampled to 5M read pairs, aligned with `--meth`.",
        notes=(
            "Compared against bwameth, not bwa-mem2, and run only on m7i (the methylation "
            "index needs a 64 GB host)."
        ),
    ),
    Dataset(
        sample="wgs-5M-alt",
        title="Whole genome, ALT-aware (HG00096)",
        source="The wgs-5M reads aligned with the reference's `.alt` file present.",
        notes="Measured at one rep per architecture by design; timings are indicative only.",
    ),
)

# Short CPU descriptions for every host type the sweep or arena has used.
ARCH_CPUS: dict[str, str] = {
    "c6a": "AMD EPYC Milan (Zen 3), AVX2",
    "c7a": "AMD EPYC Genoa (Zen 4), AVX-512",
    "c7i": "Intel Sapphire Rapids, AVX-512",
    "m7i": "Intel Sapphire Rapids, AVX-512",
    "c7g": "AWS Graviton3, NEON",
    "c8g": "AWS Graviton4, NEON",
    "c8a": "AMD EPYC Turin (Zen 5), AVX-512",
    "m8a": "AMD EPYC Turin (Zen 5), AVX-512",
    "m8g": "AWS Graviton4, NEON",
    "c8g64": "AWS Graviton4, NEON (64 vCPU)",
}


def release_label_to_version(label: str) -> str:
    """Map an arena ladder label to a release version (``v0110`` -> ``v0.11.0``).

    A label that is not a release tag is a blessed interim build between two
    releases (e.g. ``394f8f8``) and is marked as such.
    """
    match = _RELEASE_LABEL.match(label)
    if not match:
        return f"{label} (interim build)"
    return f"v0.{int(match.group(1))}.{match.group(2)}"


def _present(value: object) -> bool:
    """True for a real number; False for None and for NaN (pandas' NULL)."""
    return value is not None and not (isinstance(value, float) and math.isnan(value))


def _median(values: list[Any]) -> float | None:
    clean = [v for v in values if _present(v)]
    return statistics.median(clean) if clean else None


def _cv_pct(values: list[Any]) -> float | None:
    """Coefficient of variation in percent, or None with fewer than two reps."""
    clean = [v for v in values if _present(v)]
    if len(clean) < 2:  # noqa: PLR2004
        return None
    mean = statistics.fmean(clean)
    return statistics.stdev(clean) / mean * 100.0 if mean else None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _trial_stats(rows: list[dict[str, Any]], threads: int) -> dict[str, Any]:
    """Median wall / PROCESS() / RSS / CPU efficiency across one cell's reps."""
    walls = [r["wall_seconds"] for r in rows]
    effs = [
        r["cpu_time"] / (r["wall_seconds"] * threads) * 100.0
        for r in rows
        if _present(r["cpu_time"]) and _present(r["wall_seconds"]) and r["wall_seconds"]
    ]
    rss = [r["max_rss_mb"] / 1024.0 for r in rows if _present(r["max_rss_mb"])]
    return {
        "n": len([w for w in walls if _present(w)]),
        "wall_s": _round(_median(walls), 2),
        "wall_cv_pct": _round(_cv_pct(walls), 1),
        "process_s": _round(_median([r["process_seconds"] for r in rows]), 2),
        "rss_gb": _round(_median(rss), 1),
        "cpu_efficiency_pct": _round(_median(effs), 1),
    }


def _trials(db_path: Path, sha: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    df = query_df(
        db_path,
        """
        SELECT sample, arch, rep, wall_seconds, process_seconds, max_rss_mb, cpu_time,
               reads_processed
        FROM trials
        WHERE fg_labs_sha = ? AND wall_seconds IS NOT NULL AND wall_seconds > 0
        ORDER BY sample, arch, rep
        """,
        params=(sha,),
    )
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in df.to_dict("records"):
        cells.setdefault((row["sample"], row["arch"]), []).append(row)
    return cells


def _comparisons(db_path: Path, sha: str) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    df = query_df(
        db_path,
        """
        SELECT t.sample, t.arch, c.kind, c.concordance_pct, c.total, c.by_class_json,
               c.placement_json
        FROM comparisons c JOIN trials t ON t.id = c.trial_id
        WHERE t.fg_labs_sha = ?
        ORDER BY t.sample, t.arch, t.rep
        """,
        params=(sha,),
    )
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in df.to_dict("records"):
        cells.setdefault((row["sample"], row["kind"], row["arch"]), []).append(row)
    return cells


def _json(blob: object) -> dict[str, Any]:
    """Decode a JSON column, treating NULL as empty.

    pandas hands back NaN, not None, for a NULL in a column that also holds
    strings, so a plain truthiness check is not enough.
    """
    return json.loads(blob) if isinstance(blob, str) and blob else {}


def _by_class_median(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Median percentage of reads in each compare-bams difference class."""
    per_class: dict[str, list[float]] = {}
    for row in rows:
        for name, value in _json(row["by_class_json"]).items():
            if isinstance(value, dict) and "pct" in value:
                per_class.setdefault(name, []).append(float(value["pct"]))
    return {name: round(statistics.median(pcts), 4) for name, pcts in sorted(per_class.items())}


def _comparison_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Median / min concordance for one (sample, kind, arch) cell, plus its breakdown."""
    conc = [r["concordance_pct"] for r in rows if r["concordance_pct"] is not None]
    relocated = [
        v for r in rows if (v := _json(r["placement_json"]).get("relocated_pct")) is not None
    ]
    return {
        "n": len(rows),
        "concordance_pct": _round(_median(conc), 4),
        "min_concordance_pct": _round(min(conc), 4) if conc else None,
        "by_class_pct": _by_class_median(rows),
        "confident_relocated_pct": _round(_median(relocated), 4) if relocated else None,
    }


def _dataset_snapshot(
    dataset: Dataset,
    *,
    trials: dict[tuple[str, str], list[dict[str, Any]]],
    baseline: dict[tuple[str, str], list[dict[str, Any]]],
    comparisons: dict[tuple[str, str, str], list[dict[str, Any]]],
    config: WorkflowConfig,
) -> dict[str, Any] | None:
    """Speed, resources and agreement for one dataset, or None if it was not run."""
    archs = sorted({arch for (sample, arch) in trials if sample == dataset.sample})
    if not archs:
        return None
    sample_cfg = config.samples[dataset.sample]
    threads = config.threads

    speed = []
    for arch in archs:
        arms = {}
        for arm, suffix in _ARM_SUFFIXES.items():
            rows = trials.get((dataset.sample + suffix, arch))
            if rows:
                arms[arm] = _trial_stats(rows, threads)
        base_rows = baseline.get((dataset.sample, arch))
        comparator = _trial_stats(base_rows, threads) if base_rows else None
        speed.append({"arch": arch, "arms": arms, "comparator": comparator})

    reads = trials[(dataset.sample, archs[0])][0]["reads_processed"]
    agreement: dict[str, dict[str, Any]] = {}
    for kind, sample in (
        (_VS_BASELINE, dataset.sample),
        (_VS_GOLDEN, dataset.sample),
        (_VS_X86, dataset.sample),
        (_VS_DEFAULT, dataset.sample + "-fast"),
        ("compat", dataset.sample + "-compat"),
    ):
        stored_kind = _VS_BASELINE if kind == "compat" else kind
        cells = {
            arch: _comparison_summary(rows)
            for (s, k, arch), rows in sorted(comparisons.items())
            if s == sample and k == stored_kind
        }
        if cells:
            agreement[kind] = cells

    return {
        "sample": dataset.sample,
        "title": dataset.title,
        "source": dataset.source,
        "notes": dataset.notes,
        "layout": sample_cfg.layout,
        "is_meth": sample_cfg.baseline_tool == "bwameth",
        "comparator": "bwameth" if sample_cfg.baseline_tool == "bwameth" else "bwa-mem2",
        "mem_flags": list(sample_cfg.mem_flags),
        "reads": int(reads) if reads else None,
        "speed": speed,
        "agreement": agreement,
    }


def _arena_snapshot(db_path: Path, sha: str, version: str) -> dict[str, list[dict[str, Any]]]:
    """Per-arch arena rows: every arm's medians, with ratios oriented bwa-mem3-faster > 1."""
    df = query_df(
        db_path,
        """
        SELECT arch, label, mode, wall_seconds, process_seconds, max_rss_mb, cpu_time
        FROM arena WHERE fg_labs_sha = ? ORDER BY arch, label, mode, rep
        """,
        params=(sha,),
    )
    arena: dict[str, list[dict[str, Any]]] = {}
    for arch, group in df.groupby("arch", sort=True):
        rows = []
        for (label, mode), arm in group.groupby(["label", "mode"], sort=True):
            walls = [w for w in arm["wall_seconds"].tolist() if _present(w)]
            rss = [r / 1024.0 for r in arm["max_rss_mb"].tolist() if _present(r)]
            if label in _COMPARATOR_LABELS:
                display = label
            elif label in _CANDIDATE_LABELS.values():
                display = version
            elif label.endswith("-fast"):
                display = release_label_to_version(label.removesuffix("-fast"))
            else:
                display = release_label_to_version(label)
            rows.append(
                {
                    "label": label,
                    "display": display,
                    "mode": mode,
                    "kind": (
                        "comparator"
                        if label in _COMPARATOR_LABELS
                        else "candidate"
                        if label in _CANDIDATE_LABELS.values()
                        else "release"
                    ),
                    "n": len(walls),
                    "n_skipped": len(arm) - len(walls),
                    "wall_s": _round(_median(walls), 2),
                    "rss_gb": _round(_median(rss), 1),
                }
            )
        arena[str(arch)] = rows
    return arena


def _accuracy_snapshot(db_path: Path, sha: str) -> list[dict[str, Any]]:
    """Truth-graded accuracy per (sim sample, aligner), medians across reps.

    The `-genomic` meth-scoring arms are left out: they are an internal
    scoring-mode experiment, not a configuration users run.
    """
    df = query_df(
        db_path,
        """
        SELECT sample, arch, tool, placement_correct_pct, placement_mismapped_pct,
               placement_json, md_concordant_pct, nm_concordant_pct
        FROM accuracy WHERE fg_labs_sha = ?
        ORDER BY sample, tool, rep
        """,
        params=(sha,),
    )
    out = []
    for (sample, tool), group in df.groupby(["sample", "tool"], sort=True):
        if str(sample).endswith("-genomic"):
            continue
        confident_share, confident_mismapped = [], []
        for blob in group["placement_json"]:
            bins = _json(blob).get("bins", {})
            total = sum(b["total"] for b in bins.values())
            conf = [b for key, b in bins.items() if _bin_lower_bound(key) >= _CONFIDENT_MAPQ]
            conf_total = sum(b["total"] for b in conf)
            if total and conf_total:
                confident_share.append(conf_total / total * 100.0)
                confident_mismapped.append(sum(b["mismapped"] for b in conf) / conf_total * 100.0)
        base = str(sample).removesuffix("-fast")
        out.append(
            {
                "dataset": base,
                "aligner": _accuracy_aligner(
                    str(tool), fast=str(sample).endswith("-fast"), meth=base.startswith("sim-meth")
                ),
                "arch": str(group["arch"].iloc[0]),
                "n": len(group),
                "correct_pct": _round(_median(group["placement_correct_pct"].tolist()), 2),
                "mismapped_pct": _round(_median(group["placement_mismapped_pct"].tolist()), 2),
                "confident_share_pct": _round(_median(confident_share), 2),
                "confident_mismapped_pct": _round(_median(confident_mismapped), 3),
                "md_concordant_pct": _round(_median(group["md_concordant_pct"].tolist()), 2),
                "nm_concordant_pct": _round(_median(group["nm_concordant_pct"].tolist()), 2),
            }
        )
    return out


def _bin_lower_bound(key: str) -> int:
    """Lower MAPQ bound of a holodeck bin key (``"0"``, ``"20-29"``, ``"60+"``)."""
    match = re.match(r"\d+", key)
    if match is None:
        raise ValueError(f"unrecognised MAPQ bin {key!r}")
    return int(match.group(0))


def _accuracy_aligner(tool: str, *, fast: bool, meth: bool) -> str:
    if tool == "fg-labs":
        return "bwa-mem3 --fast" if fast else "bwa-mem3"
    if tool == "baseline":
        return "bwameth" if meth else "bwa-mem2"
    return tool


def _scaling_snapshot(db_path: Path, sha: str) -> list[dict[str, Any]]:
    """Thread-scaling ladder: median PROCESS() time and pipeline efficiency per rung."""
    df = query_df(
        db_path,
        """
        SELECT sample, arch, threads, wall_seconds, process_seconds
        FROM scaling WHERE fg_labs_sha = ? ORDER BY sample, arch, threads, rep
        """,
        params=(sha,),
    )
    out: list[dict[str, Any]] = []
    for (sample, arch), group in df.groupby(["sample", "arch"], sort=True):
        rungs: list[dict[str, Any]] = []
        for threads, rung in group.groupby("threads", sort=True):
            times = [
                p if _present(p) else w  # an unparsed PROCESS() falls back to wall
                for p, w in zip(rung["process_seconds"], rung["wall_seconds"], strict=True)
            ]
            rungs.append({"threads": int(threads), "n": len(rung), "time_s": _median(times)})
        t1 = next((r["time_s"] for r in rungs if r["threads"] == 1), None)
        for rung in rungs:
            eff = t1 / (rung["threads"] * rung["time_s"]) * 100.0 if t1 and rung["time_s"] else None
            rung["efficiency_pct"] = _round(eff, 1)
            rung["time_s"] = _round(rung["time_s"], 2)
        out.append({"sample": str(sample), "arch": str(arch), "rungs": rungs})
    return out


def _previous_release(allowances: list[ReleaseAllowance], sha: str) -> ReleaseAllowance | None:
    for i, entry in enumerate(allowances):
        if sha_prefix_match(entry.to_sha, sha):
            return allowances[i - 1] if i > 0 else None
    return None


def build_snapshot(  # noqa: PLR0913
    *,
    db_path: Path,
    fg_labs_sha: str,
    version: str,
    previous_version: str,
    allowances: list[ReleaseAllowance],
    config: WorkflowConfig,
    sweep_sa_stride: int,
    arena_sa_stride: int,
) -> dict[str, Any]:
    """Assemble the full snapshot for one blessed release.

    :param fg_labs_sha: the blessed golden SHA the release was benched at.
    :param version: the release version, e.g. ``v0.12.0``.
    :param previous_version: the version of the prior blessed release (the
        vs-golden reference), for display.
    :param allowances: the release ledger; supplies the bless date and PR.
    :param sweep_sa_stride: suffix-array sampling stride of the sweep index.
    :param arena_sa_stride: suffix-array sampling stride the arena's bwa-mem3
        arms used (comparators always use the stock stride-8 index).
    :raises ValueError: if the SHA is not a blessed release or has no trials.
    """
    entry = next((a for a in allowances if sha_prefix_match(a.to_sha, fg_labs_sha)), None)
    if entry is None:
        raise ValueError(f"{fg_labs_sha} is not a blessed release in the ledger")
    trials = _trials(db_path, fg_labs_sha)
    if not trials:
        raise ValueError(f"no trials for {fg_labs_sha} in {db_path}")
    baseline = _trials(db_path, baseline_sha_for(config.upstream_tag))
    comparisons = _comparisons(db_path, fg_labs_sha)
    previous = _previous_release(allowances, entry.to_sha)

    datasets = [
        snap
        for d in DATASETS
        if (
            snap := _dataset_snapshot(
                d, trials=trials, baseline=baseline, comparisons=comparisons, config=config
            )
        )
    ]
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "version": version,
        "fg_labs_sha": entry.to_sha,
        "aliases": list(entry.aliases),
        "date": str(entry.date),
        "pr": entry.pr,
        "previous": {
            "version": previous_version,
            "fg_labs_sha": previous.to_sha if previous else None,
        },
        "threads": config.threads,
        "arena_threads": config.arena.threads,
        "arena_sample": config.arena.sample,
        "batch_bases": config.batch_bases,
        "sweep_sa_stride": sweep_sa_stride,
        "arena_sa_stride": arena_sa_stride,
        "comparators": {
            "bwa": config.bwa_version,
            "bwa-mem2": config.upstream_tag,
            "bwameth": config.bwameth_version,
            "minibwa": minibwa_sha(),
        },
        "max_reps": max(len(rows) for rows in trials.values()),
        "datasets": datasets,
        "arena": _arena_snapshot(db_path, fg_labs_sha, version),
        # Chronological ladder order, captured now so rendering never depends on
        # the ladder as it stands when a frozen release is re-rendered later.
        "arena_ladder": [label for label, _sha in arena_releases()],
        "accuracy": _accuracy_snapshot(db_path, fg_labs_sha),
        "scaling": _scaling_snapshot(db_path, fg_labs_sha),
    }
