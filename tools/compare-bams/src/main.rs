//! `compare-bams` CLI — lockstep BAM comparison (matching record order).

use std::fs::File;
use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
use compare_bams::{compare, CompareOptions};

/// Reject anything that is not a well-formed SAM aux tag name.
///
/// A SAM aux tag is exactly two characters, `[A-Za-z][A-Za-z0-9]` (`SAMv1` §1.5),
/// so `--expect-tag XYZ` or `--ignore-tag 1` can never match a tag on a record.
/// Left unchecked they are inert config: an unmatchable `--expect-tag`
/// allowlists nothing, which is precisely the silently-does-nothing failure the
/// tag guard exists to reject — so the guard's own CLI is held to the same rule.
/// Mirrors `_as_tag_list` in `bwa_mem3_bench/workflow_config.py`.
fn parse_aux_tag(value: &str) -> Result<String, String> {
    let bytes = value.as_bytes();
    if bytes.len() == 2 && bytes[0].is_ascii_alphabetic() && bytes[1].is_ascii_alphanumeric() {
        return Ok(value.to_string());
    }
    Err(format!(
        "{value:?} is not a SAM aux tag name: expected exactly two characters \
         matching [A-Za-z][A-Za-z0-9] (e.g. NM, MD, XS)"
    ))
}

#[derive(Parser, Debug)]
#[command(
    name = "compare-bams",
    about = "Compare two BAMs in lockstep (matching record order)"
)]
struct Args {
    /// Path to the query (fg-labs) BAM.
    #[arg(long)]
    query: PathBuf,

    /// Path to the baseline (upstream or golden) BAM.
    #[arg(long)]
    baseline: PathBuf,

    /// Output JSON report path.
    #[arg(long)]
    out: PathBuf,

    /// Aux tag to exclude from comparison entirely — neither its presence nor
    /// its value is compared (repeat per tag). Every tag NOT listed is compared
    /// strictly and a difference counts against concordance. Example:
    /// `--ignore-tag MQ --ignore-tag HN` against upstream bwa-mem2, which emits
    /// neither. Excluded tags are still tallied under `by_tag` in the report.
    #[arg(long = "ignore-tag", value_name = "TAG", value_parser = parse_aux_tag)]
    ignore_tags: Vec<String>,

    /// Aux tag that MAY appear (repeat per tag). Any tag observed that is on
    /// neither this list nor `--ignore-tag` fails the run, naming the tag —
    /// otherwise an unanticipated tag shows up only as an unexplained drop in
    /// concordance. Listing a tag that never appears is a harmless no-op, so one
    /// list serves samples whose tag sets legitimately differ.
    ///
    /// Required unless `--no-tag-guard` is given: an empty allowlist cannot be
    /// distinguished from an unconfigured one, so the tool would have to skip
    /// the check silently. Making the choice explicit at the CLI boundary means
    /// the guard can only be off because a caller asked for it.
    #[arg(
        long = "expect-tag",
        value_name = "TAG",
        required_unless_present = "no_tag_guard",
        value_parser = parse_aux_tag
    )]
    expect_tags: Vec<String>,

    /// `--ignore-tag` entry known to be absent from this comparison, exempting
    /// it from the dead-entry check (repeat per tag). The tag stays ignored; only
    /// the audit skips it. Used for tags absent by nature (mate tags on
    /// single-end reads) or by defect (`MQ`/`HN` under `--meth` on builds
    /// predating fg-labs/bwa-mem3#304, which closed #296).
    #[arg(long = "absent-ok-tag", value_name = "TAG", value_parser = parse_aux_tag)]
    absent_ok_tags: Vec<String>,

    /// Skip the tag-set guard entirely. For exploratory comparisons against an
    /// unfamiliar BAM pair, where the tag set is what you are trying to find out.
    #[arg(long)]
    no_tag_guard: bool,

    /// Permitted absolute MAPQ difference before classifying as discordant.
    #[arg(long, default_value_t = 0)]
    mapq_tolerance: u8,
}

/// Exit code for a tag-guard failure.
///
/// Distinct from 1 (an I/O or parse error, via `anyhow`) and 2 (clap's usage
/// error) so a caller can tell "the comparison could not run" from "the
/// comparison ran and its tag policy no longer matches reality". The latter is
/// also a different channel from the regression gates, which fail on a
/// *threshold* rather than on the shape of the output.
const EXIT_TAG_GUARD: i32 = 3;

fn main() -> Result<()> {
    let args = Args::parse();

    let opts = CompareOptions {
        ignore_tags: args.ignore_tags.into_iter().collect(),
        expect_tags: args.expect_tags.into_iter().collect(),
        absent_ok_tags: args.absent_ok_tags.into_iter().collect(),
        mapq_tolerance: args.mapq_tolerance,
        tag_guard: !args.no_tag_guard,
    };

    let query =
        File::open(&args.query).with_context(|| format!("opening {}", args.query.display()))?;
    let baseline = File::open(&args.baseline)
        .with_context(|| format!("opening {}", args.baseline.display()))?;

    let report = compare(query, baseline, &opts)?;

    // The report is written before the guard verdict is acted on, and carries
    // the violations itself: a run that fails the guard still has to be
    // diagnosable, and `by_tag` is where you diagnose it.
    let json = report.to_json().context("serializing concordance report")?;
    std::fs::write(&args.out, json).with_context(|| format!("writing {}", args.out.display()))?;
    eprintln!(
        "compare-bams: total={}, concordant={}, concordance={:.4}%",
        report.total_reads, report.concordant, report.concordance_pct,
    );

    if !report.tag_guard_violations.is_empty() {
        for violation in &report.tag_guard_violations {
            eprintln!("compare-bams: tag-guard: {}", violation.message());
        }
        eprintln!(
            "compare-bams: tag-guard: {} violation(s); report written to {}",
            report.tag_guard_violations.len(),
            args.out.display(),
        );
        std::process::exit(EXIT_TAG_GUARD);
    }

    Ok(())
}
