//! `compare-bams` CLI — lockstep BAM comparison (matching record order).

use std::fs::File;
use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
use compare_bams::{compare, CompareOptions};

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
    #[arg(long = "ignore-tag", value_name = "TAG")]
    ignore_tags: Vec<String>,

    /// Permitted absolute MAPQ difference before classifying as discordant.
    #[arg(long, default_value_t = 0)]
    mapq_tolerance: u8,
}

fn main() -> Result<()> {
    let args = Args::parse();

    let opts = CompareOptions {
        ignore_tags: args.ignore_tags.into_iter().collect(),
        mapq_tolerance: args.mapq_tolerance,
    };

    let query =
        File::open(&args.query).with_context(|| format!("opening {}", args.query.display()))?;
    let baseline = File::open(&args.baseline)
        .with_context(|| format!("opening {}", args.baseline.display()))?;

    let report = compare(query, baseline, &opts)?;

    let json = report.to_json().context("serializing concordance report")?;
    std::fs::write(&args.out, json).with_context(|| format!("writing {}", args.out.display()))?;
    eprintln!(
        "compare-bams: total={}, concordant={}, concordance={:.4}%",
        report.total_reads, report.concordant, report.concordance_pct,
    );
    Ok(())
}
