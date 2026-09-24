"""`bench results` -- render the published ``results/`` pages from release snapshots.

Layout written under the results root::

    README.md                   index: latest release's headline + every release
    methodology.md              hand-written; linked, never generated
    releases/<version>/
        data.json               the snapshot (see results_data.py)
        README.md               provenance + arena headline + dataset links
        <dataset>.md            one per dataset
        accuracy.md             truth-graded accuracy on simulated reads
        scaling.md              thread-scaling ladder

Rendering is a pure function of the snapshots, so the committed pages can be
regenerated from the committed ``data.json`` files alone and a test checks they
are current. A release's directory is frozen once written: re-publishing it
needs ``force``, because a later arena ladder or config change would otherwise
quietly rewrite a past release's numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bwa_mem3_bench.report.results_data import ARCH_CPUS
from bwa_mem3_bench.report.tables import md_table

EM_DASH = "—"

# Datasets whose sweep timings are single-rep by design (see _alt_targets).
_SINGLE_REP_DATASETS = frozenset({"wgs-5M-alt"})

# A concordance below this gets its difference breakdown shown, so a reader can
# see whether reads moved or only aux tag values changed.
_BREAKDOWN_BELOW_PCT = 99.99

# The compare-bams difference classes, in display order, with reader-facing names.
_CLASS_NAMES = {
    "pos_diff": "position",
    "cigar_diff": "CIGAR",
    "flag_diff": "FLAG",
    "mapq_diff": "MAPQ",
    "tag_diff": "aux tags",
    "mapped_only_baseline": "mapped only by reference side",
    "mapped_only_query": "mapped only by bwa-mem3",
}

_RELEASES_DIR = "releases"
_SNAPSHOT_NAME = "data.json"


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    return EM_DASH if value is None else f"{value:.{digits}f}{suffix}"


def _ratio(numerator: float | None, denominator: float | None) -> str:
    if not numerator or not denominator:
        return EM_DASH
    return f"{numerator / denominator:.2f}x"


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.lstrip("v").split(".") if p.isdigit())


# ---------------------------------------------------------------------------
# Release README
# ---------------------------------------------------------------------------


def _arena_comparator_table(rows: list[dict[str, Any]], version: str) -> str:
    by_label = {(r["label"], r["mode"]): r for r in rows}
    ours = by_label.get(("fg-labs-default", "default"))
    ours_fast = by_label.get(("fg-labs-fast", "fast"))
    ours_wall = ours["wall_s"] if ours else None
    fast_wall = ours_fast["wall_s"] if ours_fast else None
    table_rows = [
        [
            f"**bwa-mem3 {version}**",
            _fmt(ours_wall),
            "1.00x",
            _ratio(ours_wall, fast_wall),
            _fmt(ours["rss_gb"], 1) if ours else EM_DASH,
        ],
        [
            f"**bwa-mem3 {version} `--fast`**",
            _fmt(fast_wall),
            EM_DASH,
            "1.00x",
            _fmt(ours_fast["rss_gb"], 1) if ours_fast else EM_DASH,
        ],
    ]
    for label, name in (("bwa", "bwa"), ("bwa-mem2-upstream", "bwa-mem2"), ("minibwa", "minibwa")):
        row = by_label.get((label, "default"))
        if row is None or row["wall_s"] is None:
            continue
        table_rows.append(
            [
                name,
                _fmt(row["wall_s"]),
                _ratio(row["wall_s"], ours_wall),
                _ratio(row["wall_s"], fast_wall),
                _fmt(row["rss_gb"], 1),
            ]
        )
    return md_table(
        ["aligner", "median wall (s)", "bwa-mem3 speedup", "`--fast` speedup", "peak RSS (GB)"],
        table_rows,
    )


def _arena_history_table(rows: list[dict[str, Any]]) -> str:
    """Every bwa-mem3 release on the ladder, oldest first, stock and --fast."""
    stock = [r for r in rows if r["kind"] != "comparator" and r["mode"] == "default"]
    fast = {r["display"]: r for r in rows if r["kind"] != "comparator" and r["mode"] == "fast"}
    ordered = [r for r in stock if r["kind"] == "release"] + [
        r for r in stock if r["kind"] == "candidate"
    ]
    table_rows = []
    previous_wall = None
    for row in ordered:
        fast_row = fast.get(row["display"])
        fast_wall = fast_row["wall_s"] if fast_row and fast_row["n"] else None
        table_rows.append(
            [
                row["display"],
                _fmt(row["wall_s"]),
                _fmt(fast_wall),
                _ratio(previous_wall, row["wall_s"]),
                _fmt(row["rss_gb"], 1),
            ]
        )
        if row["wall_s"] is not None:
            previous_wall = row["wall_s"]
    return md_table(
        ["release", "median wall (s)", "`--fast` wall (s)", "vs previous row", "peak RSS (GB)"],
        table_rows,
    )


def _arena_order(rows: list[dict[str, Any]], ladder: list[str]) -> list[dict[str, Any]]:
    """Sort release rows into ladder (chronological) order."""
    rank = {label: i for i, label in enumerate(ladder)}
    return sorted(
        rows, key=lambda r: (rank.get(r["label"].removesuffix("-fast"), len(rank)), r["mode"])
    )


def render_release_readme(snap: dict[str, Any]) -> str:
    """The release landing page: provenance, the arena headline, and links."""
    v = snap["version"]
    prev = snap["previous"]
    lines = [
        f"# bwa-mem3 {v} benchmark results",
        "",
        f"Benchmarked at fg-labs/bwa-mem3 `{snap['fg_labs_sha']}`"
        + (f" (release tag commit `{snap['aliases'][0]}`)" if snap["aliases"] else "")
        + f", blessed {snap['date']} ({_pr_link(snap['pr'])}).",
        f"Release notes: <https://github.com/fg-labs/bwa-mem3/releases/tag/{v}>.",
        "",
        "Comparators: "
        + ", ".join(
            [
                f"bwa {snap['comparators']['bwa']}",
                f"bwa-mem2 {snap['comparators']['bwa-mem2']}",
                f"bwameth {snap['comparators']['bwameth']}",
                f"minibwa `{snap['comparators']['minibwa'][:8]}`",
            ]
        )
        + ".",
        "",
        "How these numbers were produced, and what each comparison means, is in "
        "[the methodology page](../../methodology.md).",
        "",
        "## Headline: same-host speed (arena)",
        "",
        "Every aligner below ran interleaved on one dedicated on-demand host per "
        f"architecture, aligning `{snap['arena_sample']}` with {snap['arena_threads']} "
        "threads, "
        "so these ratios are like-for-like. Speedup is the other aligner's wall time "
        f"divided by bwa-mem3's: above 1 means bwa-mem3 is faster. {_arena_index_note(snap)}",
    ]
    for arch, rows in snap["arena"].items():
        ordered = _arena_order(rows, snap["arena_ladder"])
        n_reps = max((r["n"] for r in rows), default=0)
        lines += [
            "",
            f"### {arch} ({ARCH_CPUS.get(arch, arch)}), {n_reps} reps, median",
            "",
            _arena_comparator_table(ordered, v),
            "",
            "<details><summary>Every bwa-mem3 release on this host</summary>",
            "",
            _arena_history_table(ordered),
            "",
            "</details>",
        ]
    lines += [
        "",
        "## Datasets",
        "",
        f"Speed and agreement for each dataset, across {_instance_count(snap)} AWS instance types:",
        "",
    ]
    for d in snap["datasets"]:
        lines.append(f"- [{d['sample']}]({d['sample']}.md): {d['title']}")
    lines += [
        "",
        "## Also",
        "",
        "- [Accuracy against simulated truth](accuracy.md)",
        "- [Thread scaling](scaling.md)",
        f"- Agreement with the previous release ({prev['version']}) is on each dataset page.",
        f"- Raw aggregates: [`{_SNAPSHOT_NAME}`]({_SNAPSHOT_NAME})",
        "",
    ]
    return "\n".join(lines)


def _arena_index_note(snap: dict[str, Any]) -> str:
    stride = snap["arena_sa_stride"]
    if stride == 8:  # noqa: PLR2004
        return "Every aligner used its stock index."
    return (
        f"bwa-mem3 {snap['version']} used a denser stride-{stride} suffix-array index "
        "(byte-identical output, more memory, reflected in its RSS); every other arm used "
        "its stock index."
    )


def _sweep_index_note(snap: dict[str, Any]) -> str:
    """Which suffix-array index the sweep's bwa-mem3 arms used.

    A denser index changes both wall time and RSS, so the speed and memory
    tables are uninterpretable without it.
    """
    stride = snap["sweep_sa_stride"]
    if stride == 8:  # noqa: PLR2004
        return "bwa-mem3 used its stock stride-8 suffix-array index, as did the comparator."
    return (
        f"bwa-mem3 used a denser stride-{stride} suffix-array index (byte-identical "
        "output, faster, more memory); the comparator used its stock index."
    )


def _instance_count(snap: dict[str, Any]) -> int:
    """How many distinct instance types the release's sweep ran on."""
    return len({cell["arch"] for d in snap["datasets"] for cell in d["speed"]})


def _pr_link(pr: str) -> str:
    repo, _, number = pr.partition("#")
    return f"[{pr}](https://github.com/{repo}/pull/{number})" if number else pr


# ---------------------------------------------------------------------------
# Dataset pages
# ---------------------------------------------------------------------------


def _speed_section(d: dict[str, Any], snap: dict[str, Any]) -> list[str]:
    comparator = d["comparator"]
    rows = []
    for cell in d["speed"]:
        arms = cell["arms"]
        default = arms.get("default", {})
        comp = cell["comparator"] or {}
        rows.append(
            [
                f"{cell['arch']} ({ARCH_CPUS.get(cell['arch'], '')})",
                _fmt(default.get("wall_s")),
                _fmt(default.get("wall_cv_pct"), 1, "%"),
                _fmt(arms.get("fast", {}).get("wall_s")),
                _fmt(arms.get("compat", {}).get("wall_s")),
                _fmt(comp.get("wall_s")),
                _ratio(comp.get("wall_s"), default.get("wall_s")),
                str(default.get("n", EM_DASH)),
            ]
        )
    single = d["sample"] in _SINGLE_REP_DATASETS or snap["max_reps"] < 2  # noqa: PLR2004
    lines = [
        "## Speed by instance type",
        "",
        f"Median wall-clock seconds, {snap['threads']} threads, on AWS Batch spot hosts. "
        f"{_sweep_index_note(snap)} "
        "**Indicative only**: the reps of one cell can land on different hosts, "
        f"and the {comparator} column was measured in a separate run on other hosts, so "
        "the ratio mixes code speed with host variation. The same-host "
        "[arena](README.md#headline-same-host-speed-arena) is the number to quote. "
        f"`vs {comparator}` above 1 means bwa-mem3 is faster.",
    ]
    if single:
        lines.append("")
        lines.append("_This dataset has one rep per cell, so there is no spread (CV) to show._")
    lines += [
        "",
        md_table(
            [
                "instance",
                "bwa-mem3 (s)",
                "CV",
                "`--fast` (s)",
                "`--compat` (s)",
                f"{comparator} (s)",
                f"vs {comparator}",
                "reps",
            ],
            rows,
        ),
    ]
    return lines


def _resources_section(d: dict[str, Any], snap: dict[str, Any]) -> list[str]:
    rows = []
    for cell in d["speed"]:
        arms = cell["arms"]
        comp = cell["comparator"] or {}
        rows.append(
            [
                cell["arch"],
                _fmt(arms.get("default", {}).get("rss_gb"), 1),
                _fmt(arms.get("fast", {}).get("rss_gb"), 1),
                _fmt(comp.get("rss_gb"), 1),
                _fmt(arms.get("default", {}).get("cpu_efficiency_pct"), 0, "%"),
                _fmt(arms.get("default", {}).get("process_s")),
            ]
        )
    return [
        "## Memory and CPU",
        "",
        "Peak resident memory (GB), CPU efficiency "
        f"(CPU time / (wall x {snap['threads']} threads)), and "
        "bwa-mem3's own `PROCESS()` time, which excludes index loading.",
        "",
        md_table(
            [
                "instance",
                "bwa-mem3 RSS",
                "`--fast` RSS",
                f"{d['comparator']} RSS",
                "CPU efficiency",
                "PROCESS() (s)",
            ],
            rows,
        ),
    ]


def _breakdown(by_class: dict[str, float]) -> str:
    parts = [
        f"{name} {by_class[key]:.3f}%" for key, name in _CLASS_NAMES.items() if by_class.get(key)
    ]
    return "; ".join(parts) if parts else EM_DASH


def _agreement_rows(
    cells: dict[str, dict[str, Any]], *, metric: str = "concordance"
) -> tuple[list[list[str]], bool]:
    """One row per distinct result, listing every instance that produced it.

    Output is deterministic across instance types far more often than not, so
    identical cells are merged rather than repeated. Returns the rows and
    whether any row carries a difference breakdown.
    """
    merged: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for arch, cell in cells.items():
        if metric == "relocation":
            value = _fmt(cell["confident_relocated_pct"], 3, "%")
        else:
            value = _fmt(cell["concordance_pct"], 4, "%")
        conc = cell["concordance_pct"]
        breakdown = (
            _breakdown(cell["by_class_pct"])
            if metric == "concordance" and conc is not None and conc < _BREAKDOWN_BELOW_PCT
            else ""
        )
        merged.setdefault((value, breakdown), []).append((arch, cell["n"]))
    rows = [
        [", ".join(arch for arch, _ in archs), value, str(sum(n for _, n in archs)), breakdown]
        for (value, breakdown), archs in merged.items()
    ]
    return rows, any(row[3] for row in rows)


def _agreement_section(d: dict[str, Any], snap: dict[str, Any]) -> list[str]:
    agreement = d["agreement"]
    prev = snap["previous"]["version"]
    bwa_mem2 = snap["comparators"]["bwa-mem2"]
    lines = [
        "## Agreement",
        "",
        "Concordance is the percentage of primary reads whose alignment record matches "
        f"between the two runs. Where it is below {_BREAKDOWN_BELOW_PCT}% the table breaks "
        "the differences "
        "down by class; a read counts once under each class of difference it has. See "
        "[methodology](../../methodology.md#agreement) for which tags each comparison "
        "ignores.",
    ]
    headers = ["instances", "", "cells", "differences (% of reads)"]

    def add(
        title: str,
        intro: str,
        cells: dict[str, Any] | None,
        *,
        metric: str = "concordance",
        value_header: str = "concordance",
    ) -> None:
        lines.extend(["", f"### {title}", "", intro, ""])
        if not cells:
            lines.append("_Not measured for this release._")
            return
        rows, has_breakdown = _agreement_rows(cells, metric=metric)
        if has_breakdown:
            lines.append(md_table(["instances", value_header, "cells", headers[3]], rows))
        else:
            lines.append(md_table(["instances", value_header, "cells"], [r[:3] for r in rows]))

    if d["is_meth"]:
        cells = agreement.get("vs-baseline")
        measured = cells and any(c["confident_relocated_pct"] is not None for c in cells.values())
        add(
            "vs bwameth",
            "bwa-mem3 `--meth` and bwameth score reads differently by design, so whole-record "
            "concordance is not meaningful here. The measure is **confident relocation**: the "
            "share of primary reads either aligner maps at MAPQ >= 20 that the two place at a "
            "different locus.",
            cells if measured else None,
            metric="relocation",
            value_header="confident relocation",
        )
    else:
        add(
            f"vs bwa-mem2 {bwa_mem2}",
            f"x86 instances only (bwa-mem2 {bwa_mem2} has no Arm build). Tags bwa-mem3 adds or "
            "computes differently by design are ignored.",
            agreement.get("vs-baseline"),
        )
        add(
            f"`--compat` vs bwa-mem2 {bwa_mem2}",
            "`--compat=bwa-mem2` promises output identical to bwa-mem2, with nothing ignored.",
            agreement.get("compat"),
        )
    add(
        f"vs the previous release ({prev})",
        "Every tag compared. Differences here are intentional changes in this release, "
        "described in its release notes.",
        agreement.get("vs-golden"),
    )
    if not d["is_meth"]:
        add(
            "Arm vs x86",
            "The same release on Graviton compared with its x86 output.",
            agreement.get("vs-x86"),
        )
    add(
        "`--fast` vs the default preset",
        "`--fast` deliberately prunes the candidate alignments it considers, so it is not "
        "expected to match the default preset. bwa-mem3's documentation reports that about "
        "85% of the reads it re-places had MAPQ 0 (multi-mapping); the "
        "[accuracy page](accuracy.md) measures the effect against simulated truth.",
        agreement.get("vs-default"),
    )
    return lines


def render_dataset_page(d: dict[str, Any], snap: dict[str, Any]) -> str:
    reads = d["reads"]
    unit = "read pairs" if d["layout"] == "paired" else "reads"
    count = reads // 2 if reads and d["layout"] == "paired" else reads
    lines = [
        f"# {d['sample']}: {d['title']}",
        "",
        f"bwa-mem3 {snap['version']}. [All datasets](README.md) · "
        "[methodology](../../methodology.md)",
        "",
        d["source"],
    ]
    if count:
        lines += [
            "",
            f"{count:,} {unit}, aligned to GRCh38 (hg38 analysis set with decoys and ALTs).",
        ]
    if d["notes"]:
        lines += ["", d["notes"]]
    lines += [""]
    lines += _speed_section(d, snap)
    lines += [""]
    lines += _resources_section(d, snap)
    lines += [""]
    lines += _agreement_section(d, snap)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Accuracy and scaling
# ---------------------------------------------------------------------------

_ACCURACY_TITLES = {
    "sim-wgs-place": "Whole genome, placement",
    "sim-wgs-vars": "Whole genome, variant-bearing reads",
    "sim-meth-place": "Methylation, placement",
    "sim-meth-vars": "Methylation, variant-bearing reads",
}


def render_accuracy_page(snap: dict[str, Any]) -> str:
    lines = [
        f"# Accuracy against simulated truth: bwa-mem3 {snap['version']}",
        "",
        "[All datasets](README.md) · [methodology](../../methodology.md#accuracy)",
        "",
        "Reads simulated from GRCh38 with known origins, so each alignment can be graded "
        "as correct or not. `MAPQ >= 20` shows how many reads an aligner is confident "
        "about, and how often that confidence is wrong. MD and NM columns are the share of "
        "variant-bearing reads whose MD/NM tag matches the truth.",
    ]
    for dataset, title in _ACCURACY_TITLES.items():
        rows = [r for r in snap["accuracy"] if r["dataset"] == dataset]
        if not rows:
            continue
        lines += [
            "",
            f"## {title} (`{dataset}`)",
            "",
            md_table(
                [
                    "aligner",
                    "correct",
                    "mismapped",
                    "MAPQ >= 20",
                    "mismapped at MAPQ >= 20",
                    "MD match",
                    "NM match",
                    "reps",
                ],
                [
                    [
                        r["aligner"],
                        _fmt(r["correct_pct"], 2, "%"),
                        _fmt(r["mismapped_pct"], 2, "%"),
                        _fmt(r["confident_share_pct"], 2, "%"),
                        _fmt(r["confident_mismapped_pct"], 3, "%"),
                        _fmt(r["md_concordant_pct"], 2, "%"),
                        _fmt(r["nm_concordant_pct"], 2, "%"),
                        str(r["n"]),
                    ]
                    for r in rows
                ],
            ),
        ]
    lines.append("")
    return "\n".join(lines)


def render_scaling_page(snap: dict[str, Any]) -> str:
    lines = [
        f"# Thread scaling: bwa-mem3 {snap['version']}",
        "",
        "[All datasets](README.md) · [methodology](../../methodology.md#thread-scaling)",
        "",
        "One host runs every rung, timing bwa-mem3's `PROCESS()` stage (alignment "
        "pipeline, excluding index load). Efficiency is `T(1) / (n x T(n))`. This is "
        "*pipeline* efficiency: FASTQ parsing is single-threaded and overlapped with "
        "compute, so it understates pure kernel scaling slightly at high thread counts.",
    ]
    if not snap["scaling"]:
        lines += ["", "_Not measured for this release._", ""]
        return "\n".join(lines)
    for ladder in snap["scaling"]:
        lines += [
            "",
            f"## {ladder['sample']} on {ARCH_CPUS.get(ladder['arch'], ladder['arch'])}",
            "",
            md_table(
                ["threads", "PROCESS() (s)", "efficiency", "reps"],
                [
                    [
                        str(r["threads"]),
                        _fmt(r["time_s"]),
                        _fmt(r["efficiency_pct"], 1, "%"),
                        str(r["n"]),
                    ]
                    for r in ladder["rungs"]
                ],
            ),
        ]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level index
# ---------------------------------------------------------------------------


def render_index(snapshots: list[dict[str, Any]]) -> str:
    """``results/README.md``: the latest release's headline plus every release."""
    ordered = sorted(snapshots, key=lambda s: _version_key(s["version"]), reverse=True)
    latest = ordered[0]
    v = latest["version"]
    rel = f"{_RELEASES_DIR}/{v}"
    lines = [
        "# bwa-mem3 benchmark results",
        "",
        "Published at every bwa-mem3 release bless. Each release has its own frozen page; "
        "this index always leads with the latest. How the numbers are produced is in "
        "[methodology.md](methodology.md).",
        "",
        f"## Latest: {v}",
        "",
        f"Same-host speed on `{latest['arena_sample']}` ({latest['arena_threads']} threads), "
        "median of the arena reps. "
        "Speedup is the other aligner's wall time divided by bwa-mem3's (above 1 means "
        "bwa-mem3 is faster).",
    ]
    for arch, rows in latest["arena"].items():
        lines += [
            "",
            f"### {arch} ({ARCH_CPUS.get(arch, arch)})",
            "",
            _arena_comparator_table(_arena_order(rows, latest["arena_ladder"]), v),
        ]
    lines += [
        "",
        f"Full results: [{v}]({rel}/README.md). Per dataset: "
        + ", ".join(f"[{d['sample']}]({rel}/{d['sample']}.md)" for d in latest["datasets"])
        + f", [accuracy]({rel}/accuracy.md), [thread scaling]({rel}/scaling.md).",
        "",
        "## All releases",
        "",
        md_table(
            ["release", "benchmarked at", "blessed", "reps per cell"],
            [
                [
                    f"[{s['version']}]({_RELEASES_DIR}/{s['version']}/README.md)",
                    f"`{s['fg_labs_sha'][:8]}`",
                    s["date"],
                    str(s["max_reps"]),
                ]
                for s in ordered
            ],
        ),
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def render_release(snap: dict[str, Any]) -> dict[str, str]:
    """Every page of one release, keyed by file name within its directory."""
    pages = {
        "README.md": render_release_readme(snap),
        "accuracy.md": render_accuracy_page(snap),
        "scaling.md": render_scaling_page(snap),
    }
    for d in snap["datasets"]:
        pages[f"{d['sample']}.md"] = render_dataset_page(d, snap)
    return pages


def dump_snapshot(snap: dict[str, Any]) -> str:
    """Canonical JSON text for a snapshot (sorted keys, stable indentation)."""
    return json.dumps(snap, indent=2, sort_keys=True) + "\n"


def load_snapshots(root: Path) -> list[dict[str, Any]]:
    """Every committed release snapshot under ``root/releases/*/data.json``."""
    return [
        json.loads(path.read_text())
        for path in sorted((root / _RELEASES_DIR).glob(f"*/{_SNAPSHOT_NAME}"))
    ]


def expected_files(root: Path) -> dict[Path, str]:
    """Every generated file under ``root`` and the text it should hold."""
    snapshots = load_snapshots(root)
    files: dict[Path, str] = {}
    for snap in snapshots:
        release_dir = root / _RELEASES_DIR / snap["version"]
        for name, text in render_release(snap).items():
            files[release_dir / name] = text
    if snapshots:
        files[root / "README.md"] = render_index(snapshots)
    return files


def write_release(root: Path, snap: dict[str, Any], *, force: bool = False) -> list[Path]:
    """Write one release's snapshot, then re-render every page under ``root``.

    :raises FileExistsError: if the release is already published and not ``force``.
    """
    release_dir = root / _RELEASES_DIR / snap["version"]
    snapshot_path = release_dir / _SNAPSHOT_NAME
    if snapshot_path.exists() and not force:
        raise FileExistsError(
            f"{snapshot_path} already exists; published releases are frozen. "
            "Pass --force to regenerate it from the database."
        )
    release_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(dump_snapshot(snap))
    written = [snapshot_path]
    for path, text in expected_files(root).items():
        path.write_text(text)
        written.append(path)
    return written


def rerender(root: Path) -> list[Path]:
    """Re-render every page from the committed snapshots (no database needed)."""
    files = expected_files(root)
    for path, text in files.items():
        path.write_text(text)
    return list(files)
