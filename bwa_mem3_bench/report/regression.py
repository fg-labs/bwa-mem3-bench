"""`bench regression` — gate a new SHA on concordance + perf.

Three gates:
  * Gate #1 (vs upstream bwa-mem2): observed concordance drift must stay within
    the per-sample budget declared in the divergence registry.
  * Gate #2 (vs the blessed golden / last release): concordance stays tight
    (>= CONCORDANCE_THRESHOLD) — two fg-labs builds should be ~identical.
  * Performance: median wall_seconds must not regress past the threshold.

The report aggregates wall_seconds across reps to per-(sample, arch) medians
and ranges. A perf delta only fails the gate when the new wall_s range lies
entirely above the previous range — overlapping ranges are marked `noisy`
and explicitly do not fail the gate. This keeps Sapphire Rapids spot-pool
noise (m7i / c7i, ~10-50% CV per CLAUDE.md) from masquerading as a real
SHA-vs-SHA regression while still catching clean signals on the low-CV
archs (c6a / c7a / c7g / c8g, ~1% CV).

Two kinds of cell never carry a hard perf verdict, by two DIFFERENT mechanisms.
Cells with < 2 reps on either side (a single point has no range — see
`_MIN_REPS_FOR_PERF_GATE`) are classified `noisy`: they still appear in the perf
table, but a `noisy` verdict never fails the gate. `truth: true` accuracy
samples (graded by holodeck, run one rep by design, and already excluded from
Gate #1) are dropped from the perf table entirely by `_build_perf_cells` — they
get no perf row and no verdict at all, not a `noisy` one. Neither can fail the
gate.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.registry import (
    DEFAULT_REGISTRY_PATH,
    DivergenceEntry,
    allowed_drift_pct,
    load_registry,
)
from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage import VS_BASELINE, VS_GOLDEN
from bwa_mem3_bench.storage.queries import query_df
from bwa_mem3_bench.workflow_config import load_config

# Gate #2 (vs the blessed golden / last release): two fg-labs builds should be
# ~identical, so this stays tight. Loosening it must be deliberate (re-bless +
# declared allowance).
CONCORDANCE_THRESHOLD = 99.999
PERF_REGRESSION_THRESHOLD_PCT = 5.0

# Gate #1 (vs upstream bwa-mem2): observed drift is gated against the per-sample
# budget declared in the divergence registry, not a flat threshold — workloads
# diverge from upstream by intentionally different amounts (exome ~0.0004% vs
# methylation ~1.1%). MARGIN absorbs float noise; concordance is deterministic
# per (sample), so it can be tiny.
DRIFT_MARGIN_PCT = 0.001

# Sample-stdev (ddof=1) is undefined for a single sample; report 0% CV
# instead of NaN so the markdown table stays tidy.
_MIN_REPS_FOR_CV = 2

# A perf REGRESSION verdict rests on a range-overlap test (`new_min > prev_max`),
# which needs a real range -- i.e. >= 2 reps -- on BOTH sides. With one rep a
# "range" collapses to a single point, so the overlap test degenerates into a
# bare point comparison that can never come out `noisy`: a lone draw on a high-CV
# pool (m7i / c7i) would auto-fail the gate. Cells below this on either side are
# graded `noisy` (non-gating) -- one measurement cannot separate signal from noise.
_MIN_REPS_FOR_PERF_GATE = 2

# Gate #3: the efficiency a perfectly-scaling run achieves, and how far past it
# a BASELINE may read before the gate refuses to use it as a bar.
#
# E(n) = T(1) / (n * T(n)) above 100% claims the run got more than n-fold faster
# on n threads. Superlinear speedup is real in workloads whose per-thread
# working set shrinks into cache; this is not one of those — the FMI index is
# shared and multi-gigabyte — so an excess here means T(1) is inconsistent with
# the T(n) beside it, not that the aligner beat physics.
#
# The tolerance exists because efficiency divides two measured timings, so a
# hair over 100% is ordinary noise. The effective rejection boundary is therefore
# 100 + 2 = 102%, and only a baseline reading STRICTLY above that is refused.
# Beyond it, the ladder's T(1) is not a fact worth dividing by, so gating on
# EFFICIENCY against an inflated bar — which would demand the new release also
# scale superlinearly, which no release can do — is refused. But refusing the
# efficiency bar is not the same as declining to gate: the rung still carries two
# measured absolute times, T(n)_new and T(n)_prev, and neither depends on T(1).
# So the gate falls back to comparing those directly (`basis=absolute`, see
# `_scaling_gate`) — the new release's per-rung PROCESS() time (the same compute
# basis efficiency uses, NOT end-to-end wall) may rise at most
# `PERF_REGRESSION_THRESHOLD_PCT` over the previous release's. A release faster at
# every rung passes; one slower at any rung fails; the inflated T(1) never enters
# the arithmetic. Punting the whole ladder with no verdict, as an earlier version
# did, let a real per-rung slowdown ride through unnoticed.
#
# Not hypothetical. The v0.8.0 golden recorded 101-104% at five rungs off a
# single unreplicated T(1); re-measured it came back 8.4% faster (1341.22 ->
# 1228.33 s, 3 reps, 1.3% spread). Against that EFFICIENCY bar a v0.9.0 that was
# faster at every rung in absolute terms reported a 9.81 pp REGRESSION — exactly
# the false positive the absolute fallback now converts into the pass it should
# always have been. bwa-mem3-bench#52 replicates every rung so T(1) is no longer a
# single draw; the absolute fallback is the second half of that fix, for baselines
# already recorded under the old ladder.
_PERFECT_EFFICIENCY_PCT = 100.0
_SUPERLINEAR_TOLERANCE_PP = 2.0


def _cv_pct(series: pd.Series) -> float:
    if len(series) < _MIN_REPS_FOR_CV:
        return 0.0
    mean = float(series.mean())
    if mean == 0:
        return 0.0
    return float(series.std(ddof=1) / mean * 100.0)


def _aggregate_perf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["sample", "arch", "n", "median", "min", "max", "cv_pct"])
    grouped = df.groupby(["sample", "arch"])["wall_seconds"]
    return pd.DataFrame(
        {
            "n": grouped.count(),
            "median": grouped.median(),
            "min": grouped.min(),
            "max": grouped.max(),
            "cv_pct": grouped.apply(_cv_pct),
        }
    ).reset_index()


def _classify(
    delta_pct: float,
    new_stats: tuple[float, float, float],
    prev_stats: tuple[float, float, float],
) -> str:
    """Classify a perf cell. Each ``*_stats`` is ``(min, max, n_reps)``."""
    if math.isnan(delta_pct):
        return "new_only"
    new_min, new_max, n_new = new_stats
    prev_min, prev_max, n_prev = prev_stats
    # A hard REGRESSION / improvement verdict rests on a range-overlap test, which
    # is only meaningful with >= 2 reps on both sides (see _MIN_REPS_FOR_PERF_GATE).
    # Under-sampled cells -- e.g. a compat/accuracy sample that runs one rep, or a
    # cell where spot scarcity landed only one -- are `noisy` (non-gating).
    if n_new < _MIN_REPS_FOR_PERF_GATE or n_prev < _MIN_REPS_FOR_PERF_GATE:
        return "noisy"
    if abs(delta_pct) <= PERF_REGRESSION_THRESHOLD_PCT:
        return "flat"
    if delta_pct > 0:
        return "REGRESSION" if new_min > prev_max else "noisy"
    return "improvement" if new_max < prev_min else "noisy"


def _build_perf_cells(
    new_df: pd.DataFrame, prev_df: pd.DataFrame, truth_samples: set[str]
) -> pd.DataFrame:
    """Per-(sample, arch) perf cells with medians, ranges, delta, and a `verdict`.

    ``truth: true`` accuracy samples are dropped -- they are graded by holodeck,
    not timed (see `check_regression`). Returned sorted by (arch, sample).
    """
    new_perf = _aggregate_perf(new_df[~new_df["sample"].isin(truth_samples)])
    prev_perf = _aggregate_perf(prev_df[~prev_df["sample"].isin(truth_samples)])
    cells = new_perf.merge(prev_perf, on=["sample", "arch"], suffixes=("_new", "_prev"), how="left")
    cells["delta_pct"] = (
        (cells["median_new"] - cells["median_prev"]) / cells["median_prev"]
    ) * 100.0
    cells["verdict"] = [
        _classify(d, (nmn, nmx, nn), (pmn, pmx, pn))
        for d, nmn, nmx, pmn, pmx, nn, pn in zip(
            cells["delta_pct"],
            cells["min_new"],
            cells["max_new"],
            cells["min_prev"],
            cells["max_prev"],
            cells["n_new"],
            cells["n_prev"],
            strict=False,
        )
    ]
    return cells.sort_values(["arch", "sample"]).reset_index(drop=True)


def _baseline_budget(conc_df: pd.DataFrame, registry: list[DivergenceEntry]) -> pd.DataFrame:
    """Gate #1: compare observed vs-upstream drift against each sample's budget.

    ``conc_df`` has columns ``sample, arch, baseline_concordance`` (min over
    reps). Returns the same rows plus ``observed_drift_pct``, ``allowed_drift_pct``,
    and a ``verdict`` of ``ok`` / ``over_budget``. Empty in, empty out.
    """
    cols = [
        "sample",
        "arch",
        "baseline_concordance",
        "observed_drift_pct",
        "allowed_drift_pct",
        "verdict",
    ]
    if conc_df.empty:
        return pd.DataFrame(columns=cols)
    out = conc_df.copy()
    out["observed_drift_pct"] = 100.0 - out["baseline_concordance"]
    out["allowed_drift_pct"] = out["sample"].map(lambda s: allowed_drift_pct(registry, s))
    out["verdict"] = [
        "over_budget" if obs > allowed + DRIFT_MARGIN_PCT else "ok"
        for obs, allowed in zip(out["observed_drift_pct"], out["allowed_drift_pct"], strict=False)
    ]
    return out.sort_values(["sample", "arch"]).reset_index(drop=True)[cols]


def _missing_baseline_cells(
    db_path: Path, new_df: pd.DataFrame, baseline_conc: pd.DataFrame
) -> pd.DataFrame:
    """(sample, arch) cells that should have a vs-baseline comparison but don't.

    "Should" = the cell exists in this run, has an upstream baseline counterpart
    (a ``baseline-*`` trial), and is a sample that compare-bams actually runs on.
    Intersecting with the baseline run's cells excludes ARM archs, which
    legitimately have no upstream comparison.

    ``truth: true`` samples are excluded for a different reason, and it is not
    interchangeable with the ARM one: they DO have baseline alignments, so the
    intersection keeps them, but no rule ever writes their vs-baseline JSON.
    They are graded by holodeck against simulated truth instead — `SWEEP_SAMPLES`
    excludes them and `_accuracy_targets` requests only `eval/*`. Without this
    the gate demands an artifact the workflow is designed never to produce.

    Truth samples enter a run only through the targets that request
    `_accuracy_targets` — `bless_release`, `accuracy`, `accuracy_smoke` and
    `fast`. `rule all`, the routine cross-SHA regression sweep, excludes them
    via `SWEEP_SAMPLES`, which is why this never fired there and surfaced first
    on a release bless.
    """
    empty = pd.DataFrame(columns=["sample", "arch"])
    baseline_cells = query_df(
        db_path,
        "SELECT DISTINCT sample, arch FROM trials WHERE fg_labs_sha LIKE 'baseline-%'",
    )
    if baseline_cells.empty:
        return empty  # no baseline data to define expectations against
    config = load_config(Path(REPO_ROOT) / "config")
    truth_samples = {name for name, s in config.samples.items() if s.truth}
    new_df = new_df[~new_df["sample"].isin(truth_samples)]
    new_cells = new_df[["sample", "arch"]].drop_duplicates()
    expected = new_cells.merge(baseline_cells, on=["sample", "arch"])
    present = (
        baseline_conc[["sample", "arch"]].drop_duplicates() if not baseline_conc.empty else empty
    )
    merged = expected.merge(present, on=["sample", "arch"], how="left", indicator=True)
    return merged[merged["_merge"] == "left_only"][["sample", "arch"]].reset_index(drop=True)


def _scaling_efficiency(db_path: Path, sha: str) -> pd.DataFrame:
    """Per-thread-count strong-scaling efficiency for one run.

    ``E(n) = T(1) / (n * T(n))`` using the aligner's own PROCESS() time, falling
    back to wall when PROCESS() could not be parsed.

    An earlier revision of this docstring said the overhead outside ``process()``
    "grows with thread count (measured 1.70 s at t=16 to 3.80 s at t=64)" and
    that its mechanism was unknown. Both halves were wrong, and the growth was a
    measurement artifact. Re-measured on c8g.16xlarge with the index page cache
    explicitly warmed, 2 reps, spread <=0.02 s:

        threads   wall     main_mem   PROCESS   outside PROCESS
           16     87.76 s   86.70 s   86.43 s        1.33 s
           32     46.39 s   45.29 s   45.01 s        1.38 s
           64     26.87 s   25.79 s   25.52 s        1.35 s

    The term is FLAT, not growing, and it decomposes as ~1.00 s of ``sleep(1)``
    in ``main()`` (the TSC-frequency calibration, fixed upstream in
    fg-labs/bwa-mem3#295), ~0.25 s of warm index load, and ~0.10 s of everything
    else. The original 1.70 -> 3.80 s reading is best explained by cold or
    partially-warm index I/O leaking into the runs: a cold index read costs
    11.09 s against 0.25 s warm, so even a fraction of one landing in a rung
    produces exactly that kind of spurious, noisy growth.

    PROCESS() is still the right basis, but for a different reason than stated
    before: not because it hides a term that would otherwise dominate the
    high-thread rungs, but because a constant serial term is Amdahl overhead
    that sits outside the measured ``PROCESS()`` interval and so says nothing
    about the aligner's parallel scaling. It is aligner *startup* cost, not
    harness cost -- as the decomposition above shows, it is bwa-mem3's own
    ``main()`` doing TSC calibration and index load. It is therefore real cost
    the user pays, and it stays in the end-to-end wall-time budget; it is
    excluded from the SCALING metric only, which is what this function computes.
    For the record, that constant accounts for only ~21 % of the observed
    16->64 scaling loss; the other ~79 % is inside PROCESS(), which is to say
    the gate does see the great majority of it. (Measured against ideal 4x
    scaling from t=16: 4.93 s of excess, of which the flat term is 1.02 s and
    PROCESS() is 3.91 s.)

    What PROCESS() *does* include is the serial FASTQ reader and BAM writer —
    which is why the efficiency it yields is a whole-pipeline number, not a
    kernel-parallelism number. Measured on c8g.16xlarge / wgs-5M (bare metal,
    index pinned in /dev/shm, 2 reps, spread <0.1 %). This is a SEPARATE session
    from the table above (different index staging), so its absolute numbers do
    not line up with it — PROCESS() at t=16 reads 93.52 s here against 86.43 s
    there. Compare deltas within a table, never levels across the two:

        threads   read stage   compute step   write stage   PROCESS
           16        7.27 s        92.65 s        2.59 s      93.52 s
           32        7.12 s        46.51 s        2.65 s      48.10 s
           64        7.25 s        24.21 s        2.78 s      25.82 s

    The read stage is FLAT — it is a single-threaded decompress+parse (see
    ``src/fast_reader.c``; disk wait is only 0.12 s of it with warm cache), so
    it does not scale and never will. But bwa-mem3's 3-step pipeline overlaps
    it with compute, so it is not additive: at t=64 only ~1.6 s of read+write
    is left unhidden as pipeline fill/drain. Reading is therefore ~6 % of
    PROCESS() at t=64, NOT the ~50 % a naive "read IO lives inside PROCESS()"
    reading would suggest.

    Consequence for this gate: efficiency from PROCESS() understates pure
    kernel scaling by ~5 pp at 64 threads (95.3 % kernel vs 90.5 % PROCESS over
    the 16->64 span). That gap is a property of the aligner's pipeline, not of
    any one release, so a release-over-release comparison still compares like
    with like and the gate remains valid. Do not, however, describe the number
    it produces as kernel efficiency.

    Ceiling: the flat read stage becomes the binding constraint once the
    compute step drops below it, which on this host extrapolates to t ~ 256.
    Re-validate the phase breakdown before extending the ladder past ~128
    threads, or the gate starts measuring the reader instead of the aligner.

    Returns an empty frame when the run has no ladder, so callers can no-op.
    """
    df = query_df(
        db_path,
        """
        SELECT sample, arch, threads, wall_seconds, process_seconds
        FROM scaling WHERE fg_labs_sha = ?
        """,
        params=(sha,),
    )
    if df.empty:
        return df
    df["t"] = df["process_seconds"].fillna(df["wall_seconds"])
    # A rep with neither PROCESS() nor a wall time (both stored NULL when the
    # rung's timing could not be parsed) leaves NaN, and every downstream
    # comparison reads NaN as "no regression" — `NaN > tolerance` is False, so a
    # rung with no data would render as `ok` and quietly pass the gate. Drop
    # those reps instead; a rung with no usable rep then vanishes from the gate,
    # the same way a ladder with no T(1) does.
    df = df[df["t"].notna()]
    med = df.groupby(["sample", "arch", "threads"])["t"].median().reset_index()
    out = []
    for (sample, arch), grp in med.groupby(["sample", "arch"]):
        base = grp.loc[grp["threads"] == 1, "t"]
        if base.empty or base.iloc[0] <= 0:
            # No 1-thread rung -> efficiency is undefined. Config validation
            # requires one, so this only happens on a truncated ladder.
            continue
        t1 = float(base.iloc[0])
        for _, row in grp.iterrows():
            n = int(row["threads"])
            if n <= 1:
                continue
            out.append(
                {
                    "sample": sample,
                    "arch": arch,
                    "threads": n,
                    "efficiency_pct": t1 / (n * float(row["t"])) * 100.0,
                    # The rung's own median T(n) on the SAME compute basis the
                    # efficiency above uses (`t` = PROCESS() time, wall only as a
                    # fallback when PROCESS() is unparseable), carried so the gate
                    # can fall back to an absolute per-rung comparison when the
                    # baseline's efficiency is too inflated to divide by. Grading
                    # the fallback on PROCESS() (not wall) keeps it measuring the
                    # same quantity as the efficiency basis it substitutes for; a
                    # wall-only regression is out of scope for the scaling gate,
                    # exactly as it is for the efficiency metric. See `_scaling_gate`.
                    "t_s": float(row["t"]),
                }
            )
    return pd.DataFrame(out)


def _unusable_baselines(prev: pd.DataFrame) -> set[tuple[str, str]]:
    """(sample, arch) ladders whose efficiency is too high to divide by.

    Rejection is per LADDER rather than per rung, because `T(1)` is the divisor
    at every rung: the rung that reads superlinear is the symptom, and every
    other rung of that same ladder carries the identical inflated divisor. v0.8.0
    is the worked example — its t=64 read 98.4%, under 100 only because real
    scaling loss at 64 threads ate the inflation, so gating against that rung
    would have been exactly as wrong as gating against its visibly-superlinear
    t=16.

    Derived from the COMPLETE previous ladder, deliberately not from the
    new-vs-prev join. The join keeps only thread counts both releases measured,
    so judging on it would make the rejection depend on which rungs happen to
    overlap — and a trimmed or retuned new ladder (`--ladder` is configurable)
    would then hide the evidence while leaving the bad `T(1)` in play.
    """
    return {
        key
        for key, grp in prev.groupby(["sample", "arch"])
        if grp["efficiency_pct"].max() > _PERFECT_EFFICIENCY_PCT + _SUPERLINEAR_TOLERANCE_PP
    }


def _scaling_gate(
    db_path: Path,
    new_sha: str,
    prev_sha: str,
    max_drop_pp: float,
    perf_threshold_pct: float = PERF_REGRESSION_THRESHOLD_PCT,
) -> pd.DataFrame:
    """Gate #3: thread-scaling must not regress vs the last release.

    Gated release-over-release rather than against absolute targets: an absolute
    floor would be a guess, while the previous release is a measured fact. Same
    principle as Gate #2.

    Returns an empty frame when either run lacks a ladder — notably the FIRST
    run after this gate is introduced, which has no predecessor to compare
    against and must not fail closed for that reason alone.

    Two comparison BASES, chosen per ladder:

    - `efficiency`: the default. `E(n) = T(1)/(n*T(n))` may drop at most
      `max_drop_pp` vs the previous release.
    - `absolute`: the fallback when the baseline's efficiency exceeds 100% by
      more than `_SUPERLINEAR_TOLERANCE_PP` (reads above 102%). Such a ladder's
      `T(1)` is inconsistent with the `T(n)` beside it, so its efficiency is not
      a bar worth dividing by (see `_unusable_baselines`). Rather than punt the
      whole ladder with no verdict — which lets a genuine per-rung slowdown pass
      unnoticed — the gate compares ABSOLUTE per-rung times: the new release's
      `T(n)` may exceed the previous release's by at most `perf_threshold_pct`
      at each shared rung. `T(n)` is PROCESS() time (the same compute basis the
      efficiency metric uses), NOT end-to-end wall — so the fallback measures the
      same quantity as the efficiency bar it stands in for, and a wall-only
      regression is out of scope for the scaling gate just as it is for the
      efficiency metric. This is the honest comparison an inflated efficiency
      hides: a release faster at every rung passes, and one slower at any rung
      fails, without either side's `T(1)` entering the arithmetic.

    The `basis` column records which rule graded each rung.
    """
    new = _scaling_efficiency(db_path, new_sha)
    prev = _scaling_efficiency(db_path, prev_sha)
    if new.empty or prev.empty:
        return pd.DataFrame()
    merged = new.merge(prev, on=["sample", "arch", "threads"], suffixes=("_new", "_prev"))
    if merged.empty:
        return merged
    merged["drop_pp"] = merged["efficiency_pct_prev"] - merged["efficiency_pct_new"]
    # Absolute per-rung slowdown: positive means the new release is slower.
    merged["abs_delta_pct"] = (merged["t_s_new"] - merged["t_s_prev"]) / merged["t_s_prev"] * 100.0
    unusable = _unusable_baselines(prev)
    bases: list[str] = []
    verdicts: list[str] = []
    for sample, arch, drop, abs_delta in zip(
        merged["sample"],
        merged["arch"],
        merged["drop_pp"],
        merged["abs_delta_pct"],
        strict=True,
    ):
        if (sample, arch) in unusable:
            bases.append("absolute")
            verdicts.append("REGRESSION" if abs_delta > perf_threshold_pct else "ok")
        else:
            bases.append("efficiency")
            verdicts.append("REGRESSION" if drop > max_drop_pp else "ok")
    merged["basis"] = bases
    merged["verdict"] = verdicts
    return merged.sort_values(["sample", "arch", "threads"]).reset_index(drop=True)


def _scaling_section(scaling: pd.DataFrame) -> list[str]:
    """Markdown for Gate #3, or nothing when neither run carried a ladder."""
    if scaling.empty:
        return []
    rejected = sorted(
        {
            f"{s}/{a}"
            for s, a, b in zip(scaling["sample"], scaling["arch"], scaling["basis"], strict=True)
            if b == "absolute"
        }
    )
    reject_pct = _PERFECT_EFFICIENCY_PCT + _SUPERLINEAR_TOLERANCE_PP
    note = (
        [
            "",
            f"> **Efficiency baseline rejected for {', '.join(rejected)}.** The previous "
            f"release's ladder reads above {reject_pct:.0f}% "
            "efficiency, i.e. more than n-fold faster on n threads. That means its T(1) is "
            "inconsistent with the T(n) beside it, and since T(1) is the divisor at every rung "
            "it inflates the whole ladder — so its efficiency is not a bar worth dividing by. "
            "These rungs are gated on `basis=absolute` instead: the new release's per-rung "
            "PROCESS() time (`new_s`, the same compute basis the efficiency metric uses — NOT "
            "end-to-end wall) may exceed the previous release's (`prev_s`) by at most the "
            "performance-regression threshold, with neither side's T(1) in the arithmetic.",
        ]
        if rejected
        else []
    )
    return [
        "## Gate #3 — thread-scaling vs last release",
        "",
        md_table(
            [
                "sample",
                "arch",
                "threads",
                "prev_eff_%",
                "new_eff_%",
                "drop_pp",
                "prev_s",
                "new_s",
                "abs_Δ_%",
                "basis",
                "verdict",
            ],
            scaling[
                [
                    "sample",
                    "arch",
                    "threads",
                    "efficiency_pct_prev",
                    "efficiency_pct_new",
                    "drop_pp",
                    "t_s_prev",
                    "t_s_new",
                    "abs_delta_pct",
                    "basis",
                    "verdict",
                ]
            ]
            .to_records(index=False)
            .tolist(),
            float_fmt="{:.2f}",
        ),
        *note,
        "",
    ]


def check_regression(
    *,
    db_path: Path,
    new_sha: str,
    prev_sha: str,
) -> tuple[bool, str]:
    """Return ``(passes, markdown_report)``.

    Gate #1 (vs upstream): per-(sample) observed drift (100 - vs-baseline
    concordance) fails when it exceeds the sample's registry budget. Gate #2
    (vs golden): per-rep vs-golden concordance below the threshold fails its
    cell. Perf gating uses per-(sample, arch) median walls — a cell fails only
    when the median is over the regression threshold AND the new wall_s range
    lies entirely above the prev range (no overlap).
    """
    new_df = query_df(
        db_path,
        """
        SELECT t.sample, t.arch, t.rep, t.wall_seconds,
               c.concordance_pct AS golden_concordance
        FROM trials t
        LEFT JOIN comparisons c
          ON c.trial_id = t.id AND c.kind = ?
        WHERE t.fg_labs_sha = ?
        """,
        params=(VS_GOLDEN, new_sha),
    )
    prev_df = query_df(
        db_path,
        "SELECT sample, arch, rep, wall_seconds FROM trials WHERE fg_labs_sha = ?",
        params=(prev_sha,),
    )

    if new_df.empty:
        return False, f"# Regression gate: FAIL\n\n_No trials for {new_sha}._\n"

    # `truth: true` samples (sim-*) are ACCURACY benchmarks graded by holodeck
    # against simulated truth, not wall-time perf targets -- they run one rep by
    # design and are already excluded from Gate #1 (`_missing_baseline_cells`).
    # Exclude them from the perf gate too, for the same reason: a noisy single-rep
    # wall on m7i would otherwise fail a bless on a sample never meant to be timed.
    config = load_config(Path(REPO_ROOT) / "config")
    truth_samples = {name for name, s in config.samples.items() if s.truth}
    cells = _build_perf_cells(new_df, prev_df, truth_samples)

    conc = (
        new_df[new_df["golden_concordance"].notna()]
        .groupby(["sample", "arch"])["golden_concordance"]
        .min()
        .reset_index()
    )
    concordance_fails = conc[conc["golden_concordance"] < CONCORDANCE_THRESHOLD]
    perf_fails = cells[cells["verdict"] == "REGRESSION"]

    # Gate #1: vs-upstream drift vs the per-sample registry budget.
    registry = load_registry(DEFAULT_REGISTRY_PATH)
    baseline_conc = query_df(
        db_path,
        """
        SELECT t.sample, t.arch, MIN(c.concordance_pct) AS baseline_concordance
        FROM trials t
        JOIN comparisons c ON c.trial_id = t.id AND c.kind = ?
        WHERE t.fg_labs_sha = ?
        GROUP BY t.sample, t.arch
        """,
        params=(VS_BASELINE, new_sha),
    )
    budget = _baseline_budget(baseline_conc, registry)
    budget_fails = budget[budget["verdict"] == "over_budget"]

    # Gate #1 fails *closed*: every cell that has an upstream baseline counterpart
    # must have produced a vs-baseline comparison. Intersecting the run's cells
    # with the baseline run's cells excludes ARM archs (no upstream baseline), so
    # a genuinely-missing x86/meth comparison fails rather than silently passing.
    missing_baseline = _missing_baseline_cells(db_path, new_df, baseline_conc)

    # Gate #3: thread-scaling efficiency vs the last release. Empty (and thus
    # non-failing) when either run has no ladder — including the first run after
    # this gate lands, which has no predecessor and must not fail for that.
    scaling_cfg = load_config(Path(REPO_ROOT) / "config").thread_scaling
    scaling = _scaling_gate(
        db_path,
        new_sha,
        prev_sha,
        scaling_cfg.max_efficiency_drop_pp,
        PERF_REGRESSION_THRESHOLD_PCT,
    )
    scaling_fails = (
        scaling[scaling["verdict"] == "REGRESSION"] if not scaling.empty else pd.DataFrame()
    )

    failing = (
        not concordance_fails.empty
        or not perf_fails.empty
        or not budget_fails.empty
        or not missing_baseline.empty
        or not scaling_fails.empty
    )
    verdict = "FAIL" if failing else "PASS"

    lines: list[str] = [
        f"# Regression gate: {verdict}",
        "",
        f"- Gate #1 (vs upstream): observed drift must stay within the "
        f"per-sample registry budget (margin {DRIFT_MARGIN_PCT}%).",
        f"- Gate #2 (vs golden / last release): concordance ≥ {CONCORDANCE_THRESHOLD}%.",
        f"- Gate #3 (thread scaling): efficiency may drop at most "
        f"{scaling_cfg.max_efficiency_drop_pp} pp vs the last release; when the "
        f"baseline's efficiency is unusable (super-linear), per-rung PROCESS() time "
        f"(the same compute basis the efficiency metric uses) may rise at most "
        f"{PERF_REGRESSION_THRESHOLD_PCT}% instead.",
        f"- Performance regression threshold: ≤ {PERF_REGRESSION_THRESHOLD_PCT}%",
        "- Reps aggregated by median; perf verdict is `REGRESSION` /",
        "  `improvement` only when the new and prev wall_s ranges do **not**",
        "  overlap (otherwise `noisy`, which does not fail the gate). Cells with",
        f"  < {_MIN_REPS_FOR_PERF_GATE} reps on either side are `noisy` (no range to",
        "  test); `truth:` accuracy samples are excluded from this table and the",
        "  gate entirely (not a perf target). Neither fails the gate.",
        "",
        "## Per-cell summary",
        "",
        md_table(
            [
                "sample",
                "arch",
                "n_new",
                "n_prev",
                "new_med_s",
                "new_cv%",
                "prev_med_s",
                "prev_cv%",
                "delta_%",
                "verdict",
            ],
            cells[
                [
                    "sample",
                    "arch",
                    "n_new",
                    "n_prev",
                    "median_new",
                    "cv_pct_new",
                    "median_prev",
                    "cv_pct_prev",
                    "delta_pct",
                    "verdict",
                ]
            ]
            .to_records(index=False)
            .tolist(),
            float_fmt="{:.2f}",
        ),
        "",
    ]

    if not budget.empty:
        lines.append("## Gate #1 — vs-upstream drift vs budget")
        lines.append("")
        lines.append(
            md_table(
                [
                    "sample",
                    "arch",
                    "baseline_concordance",
                    "observed_drift_%",
                    "budget_%",
                    "verdict",
                ],
                budget[
                    [
                        "sample",
                        "arch",
                        "baseline_concordance",
                        "observed_drift_pct",
                        "allowed_drift_pct",
                        "verdict",
                    ]
                ]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.4f}",
            )
        )
        lines.append("")

    if not missing_baseline.empty:
        lines.append("## Gate #1 failures — missing vs-baseline comparisons")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch"],
                missing_baseline[["sample", "arch"]].to_records(index=False).tolist(),
            )
        )
        lines.append("")

    if not budget_fails.empty:
        lines.append("## Gate #1 failures — unexplained upstream divergence")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch", "observed_drift_%", "budget_%"],
                budget_fails[["sample", "arch", "observed_drift_pct", "allowed_drift_pct"]]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.4f}",
            )
        )
        lines.append("")

    if not concordance_fails.empty:
        lines.append("## Concordance failures")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch", "min_golden_concordance"],
                concordance_fails[["sample", "arch", "golden_concordance"]]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.4f}",
            )
        )
        lines.append("")

    if not perf_fails.empty:
        lines.append("## Performance regressions")
        lines.append("")
        lines.append(
            md_table(
                ["sample", "arch", "new_med_s", "prev_med_s", "delta_%"],
                perf_fails[["sample", "arch", "median_new", "median_prev", "delta_pct"]]
                .to_records(index=False)
                .tolist(),
                float_fmt="{:.2f}",
            )
        )
        lines.append("")

    lines.extend(_scaling_section(scaling))

    return not failing, "\n".join(lines)
