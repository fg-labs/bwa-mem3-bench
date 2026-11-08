"""Gate #1b: meth placement accuracy vs sim truth, no worse than the prior release.

Gate #1 used to judge `--meth` correctness by confident relocation against
bwameth. That stopped working at bwa-mem3 v0.17.0: fg-labs/bwa-mem3#756 made
`--meth-scoring genomic` the default, which is more accurate on the bench's
simulated truth but diverges further from bwameth by design. The relocation
number is now report-only (see `docs/expected-divergences.yaml`), and this gate
takes over: each truth meth cell (holodeck-graded sim sample, bwa-mem3 arm) must
place reads at least as well as the same cell in the prior release.

Metrics, per (sample, arch), median over reps, from the per-MAPQ-bin table of
`<tool>.eval.txt` (`accuracy.placement_json`):

- ``mismapped_mapq20_ppm``: reads mismapped in bins with MAPQ >= 20, per 10^6
  reads of the ``ALL`` total. The confident-error rate; the number #756 moved.
- ``correct_mapq20_pct``: reads correct in bins with MAPQ >= 20, as % of the
  ``ALL`` total. Guards against "fewer confident errors" bought by demoting
  correct reads below MAPQ 20.

Both are normalised by the ALL total rather than by the MAPQ >= 20 subset, so a
release that demotes reads cannot make its rate look better by shrinking the
denominator.

A cell fails when ``mismapped_mapq20_ppm`` exceeds the prior's by more than
`REL_TOL` (relative) plus `ABS_TOL_PPM` (absolute), or when
``correct_mapq20_pct`` drops more than `PP_TOL` points below the prior's. Both
bounds are inclusive. Each release's median is over its own graded reps,
restricted to that release's trial reps when the cell has trials.

Fails closed. When the candidate requested any truth meth sample (has a trial
for one): every rep it has a trial for must be graded, in a new cell too; every
cell the prior release graded must be graded in the candidate; a cell the
candidate graded but the prior ran without grading fails; a compared cell's
prior must be graded on every rep the prior ran; every graded rep must have
graded as many reads as the cell's fullest grade (holodeck counts only the
records the aligner emitted, so dropped reads leave a smaller, self-consistent
table); and a rep whose table is unreadable (an ``ALL`` total of zero, bins
that do not sum to the ``ALL`` row, or a malformed table) fails its cell under
a data-error verdict naming the side at fault (``candidate_unreadable`` /
``prior_unreadable`` / ``both_unreadable``) rather than ``REGRESSION``. A
graded cell the prior release never ran (a new sim sample) is shown as
``new_only`` and not compared; the prior's ingested trials are the inventory.

A candidate with NO graded truth meth cell depends on the context, decided the
way Gate #2 decides whether it expects a golden comparison: by whether
``--golden-ref-sha`` was given (``expect_graded``). On a bless it was, and the
gate FAILS -- a release must be graded against sim truth, so an empty accuracy
table is a run or collect that went wrong, not a pass. Without it (the routine
`rule all` sweep, which requests no sim sample) the gate reports "not
evaluated" and does not fail.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from bwa_mem3_bench.report.accuracy import mapq_bin_bounds
from bwa_mem3_bench.report.tables import md_table
from bwa_mem3_bench.storage.queries import query_df
from bwa_mem3_bench.workflow_config import WorkflowConfig

# Tolerances. Sims are deterministic, so on an unchanged binary every rep and
# every release reads the same counts, and anything above zero is a real change.
# The tolerances only absorb changes too small to matter: on sim-meth-place
# (~10.7M reads, ~85-200 ppm mismapped at MAPQ >= 20) 2% relative is a few
# dozen reads, 1 ppm is ~11 reads (the floor for a cell whose prior is near 0),
# and 0.02 pp of correct-at-MAPQ>=20 is ~2,100 reads -- below the run-to-run
# churn a tie-break or seeding change produces, far below a scoring regression.
REL_TOL = 0.02
ABS_TOL_PPM = 1.0
PP_TOL = 0.02

# Far below one read in any metric here (1 read of 10^6 is 1 ppm / 1e-4 pp), so
# it only absorbs float rounding at an exact tolerance boundary.
_FLOAT_SLACK = 1e-9

# MAPQ bins at or above this lower bound count as confident -- the same cut as
# Gate #1's confident relocation and the published accuracy summary.
CONFIDENT_MAPQ = 20

# The `accuracy.tool` of the bwa-mem3 arm (the others are the comparators).
_TOOL = "fg-labs"

OK = "ok"
REGRESSION = "REGRESSION"
NEW_ONLY = "new_only"
# Data errors: a side's grade table is unreadable (an ALL total of zero, bins
# that do not sum to ALL, or a malformed table; see `placement_metrics`), so the
# cell measured nothing. They fail like a
# REGRESSION but name the side at fault, so a bad prior table is not read as the
# candidate regressing.
CANDIDATE_UNREADABLE = "candidate_unreadable"
PRIOR_UNREADABLE = "prior_unreadable"
BOTH_UNREADABLE = "both_unreadable"
FAILING_VERDICTS = frozenset({REGRESSION, CANDIDATE_UNREADABLE, PRIOR_UNREADABLE, BOTH_UNREADABLE})

_CELL_COLS = [
    "sample",
    "arch",
    "n_new",
    "n_prev",
    "mismapped_ppm_prev",
    "mismapped_ppm_new",
    "delta_mismapped_ppm",
    "limit_mismapped_ppm",
    "correct_pct_prev",
    "correct_pct_new",
    "delta_correct_pp",
    "verdict",
]


@dataclass(frozen=True)
class MethAccuracyGate:
    """Gate #1b's result.

    ``cells`` has one row per compared or new-only cell (`_CELL_COLS`);
    ``missing`` one row per cell that should have been graded and was not
    (``sample, arch, side, detail``). ``evaluated`` is False when the candidate
    has neither a trial nor a grade for any truth meth sample, in which case
    both frames are empty;
    ``expected`` records whether that is a failure (a bless, `--golden-ref-sha`)
    or merely "not evaluated" (a routine sweep).
    """

    cells: pd.DataFrame
    missing: pd.DataFrame
    evaluated: bool
    expected: bool = False

    @property
    def failing(self) -> bool:
        """Whether Gate #1b fails the regression report."""
        if not self.evaluated:
            return self.expected
        return not self.missing.empty or bool(self.cells["verdict"].isin(FAILING_VERDICTS).any())


def truth_meth_samples(config: WorkflowConfig) -> set[str]:
    """Configured samples that are both holodeck-graded (`truth`) and `--meth`."""
    return {name for name, s in config.samples.items() if s.truth and s.is_meth}


# The per-bin counts that must sum to the ALL row for a table to be whole.
_SUMMED_COUNTS = ("total", "correct", "mismapped")


def _count(value: object) -> int:
    """``value`` as a read count; ValueError unless it is a JSON integer.

    The ingest writes every count as an int, so a fraction, a boolean, or a
    number too large to parse as an integer (which JSON reads as a float) is
    malformed -- and ``int()`` would truncate it into a plausible count.
    """
    if type(value) is not int:
        raise ValueError(f"count {value!r} is not an integer")
    return value


def _counts_possible(row: dict[str, int]) -> bool:
    """Whether one bin (or the ``ALL`` row) holds counts a grader could produce.

    Each count is non-negative and the correct and mismapped reads fit in the
    row's total. Bins can sum to ``ALL`` and still fail this, as when a negative
    count in a confident bin cancels its errors.
    """
    total, correct, mismapped = (_count(row[count]) for count in _SUMMED_COUNTS)
    return min(total, correct, mismapped) >= 0 and correct + mismapped <= total


def _is_confident(key: str) -> bool:
    """Whether bin ``key`` lies wholly at or above `CONFIDENT_MAPQ`.

    Raises ValueError for a bin that straddles it (``"0-29"``, ``"10+"``): its
    reads cannot be split, and counting it either way misreports the cell.
    """
    lower, upper = mapq_bin_bounds(key)
    if lower >= CONFIDENT_MAPQ:
        return True
    if upper is not None and upper < CONFIDENT_MAPQ:
        return False
    raise ValueError(f"MAPQ bin {key!r} straddles MAPQ {CONFIDENT_MAPQ}")


def placement_metrics(placement_json: str) -> tuple[float, float]:
    """``(mismapped_mapq20_ppm, correct_mapq20_pct)`` of one eval.txt table.

    NaN for both -- which fails the cell as a data error -- when the table
    measured nothing (an ``ALL`` total of zero), is incomplete (its bins do not
    sum to the ``ALL`` row, as when a bin was lost, which would otherwise read as
    fewer errors), or is malformed (a missing field, a count that is not a JSON
    integer, a negative count, more correct plus mismapped reads than a row's
    total, or a bin label that is not wholly a MAPQ range, is reversed, or
    straddles MAPQ 20). It never raises, so one bad table cannot abort the report.
    """
    try:
        table = json.loads(placement_json)
        total = _count(table["all"]["total"])
        if total <= 0:
            return math.nan, math.nan
        for count in _SUMMED_COUNTS:
            if sum(_count(b[count]) for b in table["bins"].values()) != _count(table["all"][count]):
                return math.nan, math.nan
        if not all(_counts_possible(b) for b in (*table["bins"].values(), table["all"])):
            return math.nan, math.nan
        confident = [b for key, b in table["bins"].items() if _is_confident(key)]
        mismapped = sum(_count(b["mismapped"]) for b in confident)
        correct = sum(_count(b["correct"]) for b in confident)
    except (KeyError, TypeError, ValueError, AttributeError):
        return math.nan, math.nan
    return mismapped / total * 1e6, correct / total * 100.0


def _in_list(column: str, values: set[str]) -> tuple[str, tuple[str, ...]]:
    """A parameterised ``column IN (?, ...)`` clause and its bound values."""
    ordered = tuple(sorted(values))
    return f"{column} IN ({', '.join('?' * len(ordered))})", ordered


Cell = tuple[str, str]


def _trial_reps(db_path: Path, sha: str, samples: set[str]) -> dict[Cell, set[int]]:
    """The rep IDs one run has trials for, per (sample, arch) of ``samples``."""
    clause, names = _in_list("sample", samples)
    df = query_df(
        db_path,
        f"SELECT DISTINCT sample, arch, rep FROM trials "  # noqa: S608
        f"WHERE fg_labs_sha = ? AND {clause}",
        params=(sha, *names),
    )
    reps: dict[Cell, set[int]] = {}
    for sample, arch, rep in df.itertuples(index=False):
        reps.setdefault((str(sample), str(arch)), set()).add(int(rep))
    return reps


def _median_or_nan(values: pd.Series) -> float:
    """The median, or NaN if any value is NaN (one unreadable rep fails the cell).

    Spelled out rather than ``median(skipna=False)``: GroupBy.median takes no
    ``skipna`` before pandas 3, and the project supports pandas >= 2.0.
    """
    return math.nan if values.isna().any() else float(values.median())


ReadTotals = dict[Cell, dict[int, int]]


def _graded(
    db_path: Path, sha: str, samples: set[str], trial_reps: dict[Cell, set[int]]
) -> tuple[pd.DataFrame, dict[Cell, set[int]], ReadTotals]:
    """Per-cell medians of the two metrics, the rep IDs graded, and each rep's read total.

    A cell with trials is graded only on the reps it has trials for, so a grade
    for a rep the run never recorded cannot stand in for one it did. A rep whose
    table is unreadable (see `placement_metrics`) makes its cell's medians NaN,
    which `_verdict` reports as a data error, rather than being skipped while
    still counted. The read totals (each readable table's ``ALL`` total) feed
    the read-count check in `_missing_cells`; an unreadable table records 0, so
    a corrupt total can neither set the count the other reps are held to nor be
    reported short on top of its data error.

    Grades are keyed by (sha, sample, arch, rep, tool), not by the alignment
    they graded, so a rep re-aligned without its new grade being ingested is
    still judged on its old grade. Both come from the same SHA on the same
    simulated input; this is accepted and documented in docs/RELEASE.md.
    """
    cols = ["sample", "arch", "n", "mismapped_ppm", "correct_pct"]
    clause, names = _in_list("sample", samples)
    df = query_df(
        db_path,
        f"SELECT sample, arch, rep, placement_json FROM accuracy "  # noqa: S608
        f"WHERE fg_labs_sha = ? AND tool = ? AND {clause}",
        params=(sha, _TOOL, *names),
    )
    df = df[df["placement_json"].notna()] if not df.empty else df
    if df.empty:
        return pd.DataFrame(columns=cols), {}, {}
    keep = [
        (s, a) not in trial_reps or int(r) in trial_reps[(s, a)]
        for s, a, r in zip(df["sample"], df["arch"], df["rep"], strict=True)
    ]
    df = df[pd.Series(keep, index=df.index, dtype=bool)]
    if df.empty:
        return pd.DataFrame(columns=cols), {}, {}
    metrics = [placement_metrics(str(blob)) for blob in df["placement_json"]]
    df = df.assign(mismapped_ppm=[m for m, _ in metrics], correct_pct=[c for _, c in metrics])
    graded_reps: dict[Cell, set[int]] = {}
    totals: ReadTotals = {}
    for sample, arch, rep, blob, (mismapped_ppm, _) in zip(
        df["sample"], df["arch"], df["rep"], df["placement_json"], metrics, strict=True
    ):
        key = (str(sample), str(arch))
        graded_reps.setdefault(key, set()).add(int(rep))
        # `placement_metrics` has validated a readable table, so this cannot raise.
        readable = not math.isnan(mismapped_ppm)
        total = _count(json.loads(str(blob))["all"]["total"]) if readable else 0
        totals.setdefault(key, {})[int(rep)] = total
    grouped = df.groupby(["sample", "arch"])
    cells = pd.DataFrame(
        {
            "n": grouped["rep"].nunique(),
            "mismapped_ppm": grouped["mismapped_ppm"].agg(_median_or_nan),
            "correct_pct": grouped["correct_pct"].agg(_median_or_nan),
        }
    ).reset_index()[cols]
    return cells, graded_reps, totals


def _unreadable(*, new_bad: bool, prev_bad: bool) -> str | None:
    """The data-error verdict for a cell with an unreadable side, else None."""
    if new_bad and prev_bad:
        return BOTH_UNREADABLE
    if new_bad:
        return CANDIDATE_UNREADABLE
    if prev_bad:
        return PRIOR_UNREADABLE
    return None


def _verdict(mm_prev: float, mm_new: float, corr_prev: float, corr_new: float) -> str:
    """`OK`, `REGRESSION`, or a data-error verdict for a cell graded in both releases."""
    bad = _unreadable(
        new_bad=math.isnan(mm_new) or math.isnan(corr_new),
        prev_bad=math.isnan(mm_prev) or math.isnan(corr_prev),
    )
    if bad is not None:
        return bad  # an unreadable table measured nothing: fail closed, naming the side
    # Both bounds are inclusive; `_FLOAT_SLACK` keeps a change of exactly the
    # tolerance from tipping into a fail through float rounding.
    if mm_new > mm_prev * (1 + REL_TOL) + ABS_TOL_PPM + _FLOAT_SLACK:
        return REGRESSION
    if corr_new < corr_prev - PP_TOL - _FLOAT_SLACK:
        return REGRESSION
    return OK


def _ungraded(trials: set[int], graded: set[int]) -> str | None:
    """``k of n reps graded (missing ...)`` when some trial rep lacks a grade."""
    missing = sorted(trials - graded)
    if not missing:
        return None
    n = len(trials)
    return f"{n - len(missing)} of {n} reps graded (missing {', '.join(map(str, missing))})"


def _short_reads(
    new_totals: ReadTotals, prev_totals: ReadTotals
) -> list[tuple[str, str, str, str]]:
    """Reps that graded fewer reads than the cell's fullest grade, on either side.

    holodeck counts only the primary records the aligner emitted, so a run that
    drops reads yields a smaller table that is still self-consistent. Every rep
    of both releases aligned the same simulated input, so they must all grade
    the same number of reads; the largest total among the cell's readable
    tables is the reference. (The truth BAM's own count is not ingested, so a loss shared by
    every rep of both releases is out of reach of this check.)
    """
    rows: list[tuple[str, str, str, str]] = []
    for key in sorted(set(new_totals) | set(prev_totals)):
        sides = (("candidate", new_totals.get(key, {})), ("prior", prev_totals.get(key, {})))
        expected = max(t for _, reps in sides for t in reps.values())
        for side, reps in sides:
            for rep, total in sorted(reps.items()):
                if 0 < total < expected:  # a zero total is already a data error
                    rows.append((*key, side, f"rep {rep} graded {total} of {expected} reads"))
    return rows


def _missing_cells(  # noqa: PLR0913
    *,
    new_graded: dict[Cell, set[int]],
    prev_graded: dict[Cell, set[int]],
    new_trials: dict[Cell, set[int]],
    prev_trials: dict[Cell, set[int]],
    new_totals: ReadTotals,
    prev_totals: ReadTotals,
    expect_graded: bool,
) -> pd.DataFrame:
    """Cells or reps, on either side, left ungraded or graded on fewer reads than the input."""
    rows: list[tuple[str, str, str, str]] = []
    # Every rep the candidate ran must be graded -- including a cell new to it.
    for key in sorted(new_trials):
        if detail := _ungraded(new_trials[key], new_graded.get(key, set())):
            rows.append((*key, "candidate", detail))
    # A compared cell's prior median must cover every rep the prior ran, too.
    for key in sorted(set(prev_graded) & set(new_graded) & set(prev_trials)):
        if detail := _ungraded(prev_trials[key], prev_graded[key]):
            rows.append((*key, "prior", detail))
    # ...and every graded rep must have graded the whole input.
    # (A prior-only cell is already reported above, so only compared cells count.)
    compared_prev = {k: v for k, v in prev_totals.items() if k in new_totals}
    rows.extend(_short_reads(new_totals, compared_prev))
    for key in sorted(set(prev_graded) - set(new_graded) - set(new_trials)):
        rows.append((*key, "candidate", "graded in the prior release, not run in the candidate"))
    for key in sorted((set(new_graded) - set(prev_graded)) & set(prev_trials)):
        rows.append((*key, "prior", "the prior release ran this cell but has no grade for it"))
    # On a bless the prior is a release, which is always graded: an empty prior
    # table means it was not collected, and every cell would read new_only.
    if expect_graded and new_graded and not prev_graded:
        rows.append(("*", "*", "prior", "no truth meth cell graded in the prior release"))
    return pd.DataFrame(rows, columns=["sample", "arch", "side", "detail"])


def meth_accuracy_gate(
    db_path: Path,
    *,
    new_sha: str,
    prev_sha: str,
    config: WorkflowConfig,
    expect_graded: bool = False,
) -> MethAccuracyGate:
    """Grade every truth meth cell of ``new_sha`` against ``prev_sha``.

    :param expect_graded: the candidate must have graded truth meth cells (a
        bless: `check_regression` passes ``bool(golden_ref_sha)``). When it has
        none, the gate fails instead of reporting "not evaluated", and a prior
        release with no grade at all fails too.
    """
    missing_cols = ["sample", "arch", "side", "detail"]
    samples = truth_meth_samples(config)
    new_trials = _trial_reps(db_path, new_sha, samples) if samples else {}
    new, new_graded, new_totals = (
        _graded(db_path, new_sha, samples, new_trials) if samples else (pd.DataFrame(), {}, {})
    )
    if not new_trials and not new_graded:
        return MethAccuracyGate(
            pd.DataFrame(columns=_CELL_COLS),
            pd.DataFrame(columns=missing_cols),
            evaluated=False,
            expected=expect_graded,
        )
    prev_trials = _trial_reps(db_path, prev_sha, samples)
    prev, prev_graded, prev_totals = _graded(db_path, prev_sha, samples, prev_trials)
    if new.empty:
        new = pd.DataFrame(columns=["sample", "arch", "n", "mismapped_ppm", "correct_pct"])
    if prev.empty:
        prev = pd.DataFrame(columns=["sample", "arch", "n", "mismapped_ppm", "correct_pct"])

    merged = new.merge(prev, on=["sample", "arch"], how="left", suffixes=("_new", "_prev"))
    # A candidate-only cell the prior also RAN is a missing prior grade (reported
    # by `_missing_cells`), not a new sample; drop it here so it is not new_only.
    prev_ran = pd.Series(
        [k in prev_trials for k in zip(merged["sample"], merged["arch"], strict=True)],
        index=merged.index,
        dtype=bool,
    )
    merged = merged[merged["n_prev"].notna() | ~prev_ran].copy()
    merged["delta_mismapped_ppm"] = merged["mismapped_ppm_new"] - merged["mismapped_ppm_prev"]
    merged["limit_mismapped_ppm"] = merged["mismapped_ppm_prev"] * (1 + REL_TOL) + ABS_TOL_PPM
    merged["delta_correct_pp"] = merged["correct_pct_new"] - merged["correct_pct_prev"]
    merged["verdict"] = [
        (CANDIDATE_UNREADABLE if math.isnan(mn) or math.isnan(cn) else NEW_ONLY)
        if pd.isna(n_prev)
        else _verdict(mp, mn, cp, cn)
        for n_prev, mp, mn, cp, cn in zip(
            merged["n_prev"],
            merged["mismapped_ppm_prev"].astype(float),
            merged["mismapped_ppm_new"].astype(float),
            merged["correct_pct_prev"].astype(float),
            merged["correct_pct_new"].astype(float),
            strict=True,
        )
    ]
    cells = merged.sort_values(["sample", "arch"]).reset_index(drop=True)[_CELL_COLS]
    missing = _missing_cells(
        new_graded=new_graded,
        prev_graded=prev_graded,
        new_trials=new_trials,
        prev_trials=prev_trials,
        new_totals=new_totals,
        prev_totals=prev_totals,
        expect_graded=expect_graded,
    )
    return MethAccuracyGate(cells, missing, evaluated=True, expected=expect_graded)


def _summary(cells: pd.DataFrame) -> str:
    """One line: cells compared, failing verdicts by kind, and the worst deltas.

    A cell whose tables were unreadable has NaN deltas; it is counted (and named
    by its data-error verdict) but cannot be the "worst" by value, so it is left
    out of the extrema, which read "n/a" when no compared cell has a numeric delta.
    """
    failing = cells[cells["verdict"].isin(FAILING_VERDICTS)]
    graded = cells[cells["verdict"] != NEW_ONLY]
    if graded.empty:
        return "0 cells compared."
    counts = failing["verdict"].value_counts()
    kinds = ", ".join(f"{int(n)} {kind}" for kind, n in sorted(counts.items()))
    head = f"{len(graded)} cells" + (f" ({kinds})" if kinds else "")

    def _worst(column: str, *, largest: bool, fmt: str, unit: str) -> str:
        """The extreme numeric delta in ``column`` with its cell, or ``n/a`` if none."""
        values = graded[column].astype(float).dropna()
        if values.empty:
            return "n/a"
        row = graded.loc[values.idxmax() if largest else values.idxmin()]
        return f"{row[column]:{fmt}} {unit} ({row['sample']}/{row['arch']})"

    worst_mm = _worst("delta_mismapped_ppm", largest=True, fmt="+.2f", unit="ppm")
    worst_corr = _worst("delta_correct_pp", largest=False, fmt="+.4f", unit="pp")
    return f"{head}, worst Δmismapped {worst_mm}, worst Δcorrect {worst_corr}."


def meth_accuracy_section(gate: MethAccuracyGate, *, prev_sha: str) -> list[str]:
    """Markdown for Gate #1b."""
    out = ["## Gate #1b — meth placement accuracy vs sim truth (no worse than prior)", ""]
    if not gate.evaluated and gate.expected:
        out += [
            "**FAIL:** the candidate has no graded truth meth cell (`sim-meth-*`), although "
            "`--golden-ref-sha` marks this as a bless, which must be graded against sim "
            "truth. Run `--target bless_release` (or `accuracy`) and `collect` its "
            "accuracy rows.",
            "",
        ]
        return out
    if not gate.evaluated:
        out += [
            "Not evaluated: the candidate requested no truth meth sample (`sim-meth-*`; "
            "run `--target bless_release` or `accuracy` to produce them). On a bless "
            "(`--golden-ref-sha`) this fails.",
            "",
        ]
        return out
    out += [
        _summary(gate.cells),
        "",
        f"Per cell, each release's median over its graded trial reps, vs prior "
        f"`{prev_sha}`. `mm20_ppm` = mismapped at MAPQ >= {CONFIDENT_MAPQ} per 10^6 "
        f"reads; `corr20_%` = correct at MAPQ >= {CONFIDENT_MAPQ} as % of all reads. "
        f"Fails if `new_mm20_ppm` > `limit_ppm` (prior × {1 + REL_TOL:g} + "
        f"{ABS_TOL_PPM:g}) or `corr20_%` drops more than {PP_TOL:g} pp (both bounds "
        f"inclusive). `{NEW_ONLY}` cells were never run by the prior release and are "
        f"not compared; every candidate trial rep must still be graded, on the whole "
        f"input. `{CANDIDATE_UNREADABLE}` / `{PRIOR_UNREADABLE}` / `{BOTH_UNREADABLE}` "
        f"mark a grade table that measured nothing (zero total, bins not summing to "
        f"ALL, or malformed) on that side: a data error, which fails the gate like "
        f"`{REGRESSION}`.",
        "",
    ]
    if not gate.cells.empty:
        out += [
            md_table(
                [
                    "sample",
                    "arch",
                    "n_new",
                    "n_prev",
                    "prev_mm20_ppm",
                    "new_mm20_ppm",
                    "Δ_ppm",
                    "limit_ppm",
                    "prev_corr20_%",
                    "new_corr20_%",
                    "Δ_pp",
                    "verdict",
                ],
                gate.cells.to_records(index=False).tolist(),
                float_fmt="{:.4f}",
            ),
            "",
        ]
    if not gate.missing.empty:
        out += [
            "## Gate #1b failures — missing sim-truth grades",
            "",
            md_table(
                ["sample", "arch", "side", "detail"],
                gate.missing.to_records(index=False).tolist(),
            ),
            "",
        ]
    return out
