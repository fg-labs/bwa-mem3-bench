# Blessing a bwa-mem3 release

This is the authoritative, repeatable runbook for validating and blessing a
candidate `fg-labs/bwa-mem3` SHA as a new golden. It replaces the ad-hoc step
list that lived only in `CLAUDE.md` and the one-off `docs/0.6.0-release-validation.md`.

A bless is **part mechanical, part judgment**. The mechanical steps are
scripted by existing CLI commands; the judgment steps (declaring expected
drift, deciding the candidate is worthy, promoting to golden) are deliberately
left to a human. `cli bless-release` runs the cheap preflight and prints this
plan, but it launches nothing and promotes nothing.

## Preflight (always run first)

```bash
pixi run python -m bwa_mem3_bench.cli bless-release \
    --fg-labs-sha <candidate-sha> --golden-ref-sha <previous-release-sha>
```

This checks the invariants that otherwise waste a multi-hour run or ship a
stale comparison:

- the candidate SHA is a full 40-hex fg-labs SHA, and is **not** already the
  newest blessed release;
- `--golden-ref-sha` resolves to a blessed release in
  `docs/release-allowances.yaml`, and is the **most recent** one (comparing
  against an older golden misattributes the candidate's delta);
- the **arena ladder is consistent** — `docs/release-allowances.yaml`,
  `docker/Dockerfile.base`'s per-release `RUN` blocks, and `ARENA_RELEASES` in
  `workflow/rules/arena.smk` all agree, and the arena's prior-release arm is the
  newest blessed golden. (This is the check that would have caught v0.10.0 being
  blessed but never added to the arena ladder.)

`tests/test_arena_ladder.py` enforces the same ladder invariant in
`pixi run check`, so a stale ladder fails CI even if this preflight is skipped.

## The steps

The three named **gates** a bless exists to check (see `rule bless_release` in
`workflow/Snakefile`):

- **Gate #1 — vs-upstream:** concordance against `bwa-mem2 v2.2.1` (x86 only).
- **Gate #2 — vs-golden:** concordance against the previous blessed release
  (`--golden-ref-sha`); requires `>= 99.999%` on every golden-backed cell.
- **Gate #3 — thread scaling:** pipeline efficiency across the thread ladder.

The candidate's `release-allowances.yaml` entry is written **late** (step 7),
not first: `submit` needs only the *prior* release's allowance, and writing the
candidate's entry early would make it the newest ledger row — tripping the
preflight's "golden is the most recent blessed release" check and the
`ladder_problems` "arena-tail == newest release" invariant until the step-11
arena bump.

1. **Rebuild the base image _only if the arena ladder changed_** (a new release
   was added to `Dockerfile.base`):
   `cli build-base --image-name <ecr>-base --push`. The base tag is
   content-addressed, so this publishes a new tag and leaves old ones
   rebuildable. Skip when the ladder is unchanged.
2. **Build + push the per-SHA image:**
   `cli build --fg-labs-sha <sha> --image-name <ecr> --push`.
3. **Verify the push settled.** `aws ecr describe-images ... imageTag=<sha>` and
   match the digest `buildx` printed. Never submit before this — workers pull by
   tag and a mid-propagation submit runs the previous image.
4. **Submit the release matrix:**
   `cli submit --fg-labs-sha <sha> --target bless_release --golden-ref-sha <prev>`.
   `<prev>` is the *previous* release — already in the ledger, and the arena's
   prior-release arm.
5. **Watch to completion:** `cli watch`. **Surface spot-capacity stalls** (e.g.
   c7i / Sapphire Rapids droughts) rather than letting the coordinator hang
   silently — indefinitely-`RUNNABLE` jobs never self-fail.
6. **Collect + ingest:** `cli collect --fg-labs-sha <sha>` → `benchmark.db`.
7. **Declare expected drift.** *Now* add the candidate's entry to
   `docs/release-allowances.yaml` (`to_sha`, `pr`, `date`, `expected_drift_pct`,
   `summary`). This is a *judgment* step; the full report and `bless-golden`
   both consume it. Writing it here (not before submit) keeps the preflight and
   ladder invariants intact through the run.
8. **Check the gates:** `cli bench regression` / `cli bench full-report`. **STOP
   on any gate failure** — do not promote.
9. **Human approval.** Review the report. Blessing is a decision; there is no
   auto-promote.
10. **Promote:** `cli bless-golden --fg-labs-sha <sha>` (and
    `cli bless-baseline --upstream-tag <tag>` if the upstream tag moved).
    `bless-golden` refuses a SHA the step-7 allowance does not authorize.
11. **Bump the arena ladder for the _next_ bless.** Add this now-blessed release
    to `ARENA_RELEASES` (`arena.smk`) **and** a `RUN` block in `Dockerfile.base`,
    then rebuild the base image. `tests/test_arena_ladder.py` fails until both
    are done — that is the guard that keeps step 11 from being forgotten (it was,
    for v0.10.0).

## Why not a single auto-run button?

The step-5 stall handling and the step-9 approval are why this is a preflight +
runbook rather than a hands-off orchestrator. A **resumable auto-run
orchestrator** (drive the build→collect steps, pause at 9, resume across a
spot-drought without re-building) is the natural next slice — it would call the
same commands this runbook lists, gated by the same preflight. Deferred
deliberately: the
value ceiling on automating an infrequent, human-gated, hours-long,
capacity-bound process is lower than the cost of getting resume/gate integration
wrong.
