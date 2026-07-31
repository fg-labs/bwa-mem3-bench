//! `tag-census` — measurement instrument for the aux-tag tiering decision (bench #34).
//!
//! Walks two BAMs with the *same* template-grouped lockstep walk `compare-bams`
//! uses, classifies each paired primary on the existing core fields, and then
//! records **which aux tags differ** on that read.
//!
//! The output is deliberately policy-free: instead of deciding which tags are
//! fatal, it emits a histogram over the *set* of differing tags per read, split
//! by whether the read was already discordant on core fields. Any candidate
//! strict-tier set `S` can then be evaluated after the fact — the concordance
//! a run would have scored is the count of core-concordant reads whose
//! differing-tag set does not intersect `S` — so choosing tiers becomes
//! arithmetic on this output rather than a rebuild-and-rerun.

use std::collections::BTreeMap;
use std::fs::File;
use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
use compare_bams::classify::Discordance;
use compare_bams::{classify, template_iter, CompareOptions, Template};
use noodles_sam::alignment::record::data::field::Tag;
use noodles_sam::alignment::record_buf::data::field::Value;
use noodles_sam::alignment::record_buf::RecordBuf;

#[derive(Parser, Debug)]
#[command(name = "tag-census", about = "Per-read aux-tag divergence census")]
struct Args {
    /// Query (fg-labs) BAM.
    #[arg(long)]
    query: PathBuf,

    /// Baseline BAM (upstream, golden, or another fg-labs arch).
    #[arg(long)]
    baseline: PathBuf,

    /// Output JSON path.
    #[arg(long)]
    out: PathBuf,

    /// Label recorded in the report (e.g. `vs_baseline wgs-5M c6a`).
    #[arg(long, default_value = "")]
    label: String,

    /// Tags to ignore during comparison (repeat per tag). Example: --ignore-tag YD.
    ///
    /// Mirrors `compare-bams`' flag of the same name. The census only reports the
    /// same `core_concordance_pct` as the real run when it is given the same
    /// policy, so pass whatever the corresponding `compare-bams` invocation passes.
    #[arg(long = "ignore-tag", value_name = "TAG")]
    ignore_tags: Vec<String>,

    /// Permitted absolute MAPQ difference before a read counts as discordant.
    #[arg(long, default_value_t = 0)]
    mapq_tolerance: u8,
}

/// A tag value normalized so that equality ignores on-disk encoding width.
///
/// BAM stores integers in the narrowest type that fits, so the same logical
/// value can appear as `c`, `s`, or `i` in two files that agree completely.
/// Comparing `noodles`' `Value` variants directly would report those as
/// differences; collapsing every integer variant to `i64` first is what makes
/// the comparison value-semantic rather than encoding-semantic.
#[derive(Debug, PartialEq)]
enum Norm {
    Int(i64),
    Float(f32),
    /// `Z` (printable string) and `H` (hex byte array) are kept apart even when
    /// their bytes match: they are distinct SAM types written with distinct BAM
    /// type codes, so equal bytes under different types are a real difference
    /// rather than an encoding artifact the way `c` vs `i` is.
    Str(Vec<u8>),
    Hex(Vec<u8>),
    /// `A` (character), for the same reason: it is its own SAM type, not a
    /// one-byte integer, so `XX:A:A` and `XX:i:65` are genuinely different.
    Char(u8),
    /// Arrays (`B`) and anything else, compared via their debug rendering.
    /// No `B` tag appears in this workload; this arm exists so an unexpected
    /// one is compared rather than silently ignored.
    Other(String),
}

fn normalize(v: &Value) -> Norm {
    match v {
        Value::Int8(n) => Norm::Int(i64::from(*n)),
        Value::UInt8(n) => Norm::Int(i64::from(*n)),
        Value::Int16(n) => Norm::Int(i64::from(*n)),
        Value::UInt16(n) => Norm::Int(i64::from(*n)),
        Value::Int32(n) => Norm::Int(i64::from(*n)),
        Value::UInt32(n) => Norm::Int(i64::from(*n)),
        Value::Character(c) => Norm::Char(*c),
        Value::Float(f) => Norm::Float(*f),
        Value::String(s) => Norm::Str(s.to_vec()),
        Value::Hex(s) => Norm::Hex(s.to_vec()),
        Value::Array(array) => Norm::Other(format!("{array:?}")),
    }
}

fn tag_name(tag: Tag) -> String {
    String::from_utf8_lossy(tag.as_ref()).into_owned()
}

/// Per-tag presence tally across one side of the comparison.
#[derive(Default, serde::Serialize)]
struct Presence {
    query: u64,
    baseline: u64,
}

#[derive(Default, serde::Serialize)]
struct Census {
    label: String,
    total_primaries: u64,
    core_concordant: u64,
    core_concordance_pct: f64,

    /// How many records on each side carry each tag at all.
    tag_presence: BTreeMap<String, Presence>,

    /// Reads where a tag is present on exactly one side, per tag.
    tag_query_only: BTreeMap<String, u64>,
    tag_baseline_only: BTreeMap<String, u64>,
    /// Reads where both sides carry the tag but the values differ, per tag.
    tag_value_diff: BTreeMap<String, u64>,

    /// Histogram over the differing-tag SET per read, keyed by the sorted
    /// `|`-joined tag names (empty key = no tag differs). Split by whether the
    /// read was already discordant on core fields, because only the
    /// core-concordant half can lower `concordance_pct` when tags go strict.
    patterns_core_concordant: BTreeMap<String, u64>,
    patterns_core_discordant: BTreeMap<String, u64>,
}

fn is_primary(r: &RecordBuf) -> bool {
    let f = r.flags();
    !f.is_secondary() && !f.is_supplementary()
}

fn end_key(r: &RecordBuf) -> u8 {
    let f = r.flags();
    u8::from(f.is_first_segment()) | (u8::from(f.is_last_segment()) << 1)
}

/// Tags differing between two paired primaries, plus per-tag presence bookkeeping.
fn diff_tags(q: &RecordBuf, b: &RecordBuf, census: &mut Census) -> Vec<String> {
    let q_tags: BTreeMap<String, &Value> = q.data().iter().map(|(t, v)| (tag_name(t), v)).collect();
    let b_tags: BTreeMap<String, &Value> = b.data().iter().map(|(t, v)| (tag_name(t), v)).collect();

    for name in q_tags.keys() {
        census.tag_presence.entry(name.clone()).or_default().query += 1;
    }
    for name in b_tags.keys() {
        census
            .tag_presence
            .entry(name.clone())
            .or_default()
            .baseline += 1;
    }

    let mut differing = Vec::new();
    let mut names: Vec<&String> = q_tags.keys().chain(b_tags.keys()).collect();
    names.sort_unstable();
    names.dedup();

    for name in names {
        match (q_tags.get(name), b_tags.get(name)) {
            (Some(qv), Some(bv)) => {
                if normalize(qv) != normalize(bv) {
                    *census.tag_value_diff.entry(name.clone()).or_default() += 1;
                    differing.push(name.clone());
                }
            }
            (Some(_), None) => {
                *census.tag_query_only.entry(name.clone()).or_default() += 1;
                differing.push(name.clone());
            }
            (None, Some(_)) => {
                *census.tag_baseline_only.entry(name.clone()).or_default() += 1;
                differing.push(name.clone());
            }
            (None, None) => unreachable!("name came from one of the two maps"),
        }
    }
    differing
}

fn census_template(t: &Template, opts: &CompareOptions, census: &mut Census) {
    let mut q_primary: BTreeMap<u8, &RecordBuf> = BTreeMap::new();
    let mut b_primary: BTreeMap<u8, &RecordBuf> = BTreeMap::new();
    for r in &t.query {
        if is_primary(r) {
            q_primary.insert(end_key(r), r);
        }
    }
    for r in &t.baseline {
        if is_primary(r) {
            b_primary.insert(end_key(r), r);
        }
    }

    let mut ends: Vec<u8> = q_primary.keys().chain(b_primary.keys()).copied().collect();
    ends.sort_unstable();
    ends.dedup();

    for end in ends {
        let (q, b) = match (q_primary.get(&end), b_primary.get(&end)) {
            (Some(q), Some(b)) => (*q, *b),
            // Present on one side only: core-discordant, and every tag it
            // carries is by definition one-sided. Counted so the denominator
            // matches compare-bams exactly, and diffed against an empty record
            // so those tags still reach `tag_presence` and the one-sided
            // counters -- the report documents `tag_presence` as "how many
            // records on each side carry each tag at all", which a skipped
            // record would silently under-count. The read is already
            // core-discordant, so its pattern can never move
            // `core_concordance_pct`; naming the tags only makes the histogram
            // honest about why it diverged.
            (Some(q), None) => {
                census.total_primaries += 1;
                let differing = diff_tags(q, &RecordBuf::default(), census);
                *census
                    .patterns_core_discordant
                    .entry(differing.join("|"))
                    .or_default() += 1;
                continue;
            }
            (None, Some(b)) => {
                census.total_primaries += 1;
                let differing = diff_tags(&RecordBuf::default(), b, census);
                *census
                    .patterns_core_discordant
                    .entry(differing.join("|"))
                    .or_default() += 1;
                continue;
            }
            (None, None) => continue,
        };

        census.total_primaries += 1;
        let core_concordant = classify(q, b, opts) == Discordance::Concordant;
        let differing = diff_tags(q, b, census);
        let key = differing.join("|");

        if core_concordant {
            census.core_concordant += 1;
            *census.patterns_core_concordant.entry(key).or_default() += 1;
        } else {
            *census.patterns_core_discordant.entry(key).or_default() += 1;
        }
    }
}

fn main() -> Result<()> {
    let args = Args::parse();
    // Built from the flags rather than `CompareOptions::default()` so the census
    // mirrors whatever policy the corresponding `compare-bams` run used. Both
    // default to the workflow's current settings (no ignored tags, zero MAPQ
    // tolerance), so an unflagged run still matches today's invocation — but a
    // policy added to the workflow no longer silently desyncs the two.
    let opts = CompareOptions {
        ignore_tags: args.ignore_tags,
        mapq_tolerance: args.mapq_tolerance,
    };

    let query =
        File::open(&args.query).with_context(|| format!("opening {}", args.query.display()))?;
    let baseline = File::open(&args.baseline)
        .with_context(|| format!("opening {}", args.baseline.display()))?;

    let mut census = Census {
        label: args.label,
        ..Census::default()
    };
    for template in template_iter(query, baseline)? {
        census_template(&template?, &opts, &mut census);
    }

    #[allow(clippy::cast_precision_loss)]
    if census.total_primaries > 0 {
        census.core_concordance_pct =
            census.core_concordant as f64 / census.total_primaries as f64 * 100.0;
    }

    std::fs::write(&args.out, serde_json::to_string_pretty(&census)?)
        .with_context(|| format!("writing {}", args.out.display()))?;
    eprintln!(
        "tag-census: {} primaries, core-concordant {} ({:.4}%)",
        census.total_primaries, census.core_concordant, census.core_concordance_pct,
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use noodles_core::Position;
    use noodles_sam::alignment::record::{Flags, MappingQuality};
    use noodles_sam::alignment::record_buf::Data;

    const R1: u16 = 0x40;
    const R2: u16 = 0x80;

    fn tag(bytes: [u8; 2]) -> Tag {
        Tag::new(bytes[0], bytes[1])
    }

    /// A mapped primary at a fixed locus carrying `tags`.
    fn rec(name: &str, flags: u16, pos: usize, tags: &[(&[u8; 2], Value)]) -> RecordBuf {
        let data: Data = tags
            .iter()
            .map(|(t, v)| (tag(**t), v.clone()))
            .collect::<Vec<_>>()
            .into_iter()
            .collect();
        RecordBuf::builder()
            .set_name(name)
            .set_flags(Flags::from_bits_retain(flags))
            .set_reference_sequence_id(0)
            .set_alignment_start(Position::new(pos).unwrap())
            .set_mapping_quality(MappingQuality::new(60).unwrap())
            .set_data(data)
            .build()
    }

    fn census_for(query: Vec<RecordBuf>, baseline: Vec<RecordBuf>) -> Census {
        let t = Template {
            name: "t".to_string(),
            query,
            baseline,
        };
        let mut census = Census::default();
        census_template(&t, &CompareOptions::default(), &mut census);
        census
    }

    /// The whole point of `Norm`: BAM picks the narrowest integer type that
    /// fits, so two files that agree can still encode the same value as `c` and
    /// `i`. Comparing `Value`s directly would call that a difference.
    #[test]
    fn integer_width_does_not_make_tags_differ() {
        assert_eq!(normalize(&Value::Int8(7)), normalize(&Value::Int32(7)));
        assert_eq!(normalize(&Value::UInt8(7)), normalize(&Value::Int16(7)));
        assert_ne!(normalize(&Value::Int8(7)), normalize(&Value::Int8(8)));
    }

    /// Width collapsing is deliberate; type collapsing is not. `Z` and `H` are
    /// distinct SAM types that BAM writes with different type codes, so the same
    /// bytes under the two types are a real on-disk difference, not an encoding
    /// artifact the way `c` vs `i` is.
    #[test]
    fn string_and_hex_with_the_same_bytes_still_differ() {
        assert_ne!(
            normalize(&Value::String("7F".into())),
            normalize(&Value::Hex("7F".into()))
        );
        assert_eq!(
            normalize(&Value::String("7F".into())),
            normalize(&Value::String("7F".into()))
        );
    }

    /// Same rule for `A`: a character is its own SAM type, not a narrow integer.
    /// Folding it into `Norm::Int` would make `XX:A:A` and `XX:i:65` compare
    /// equal, which is a type difference the BAM records faithfully.
    #[test]
    fn character_and_integer_with_the_same_code_point_still_differ() {
        assert_ne!(
            normalize(&Value::Character(b'A')),
            normalize(&Value::Int32(i32::from(b'A')))
        );
        assert_eq!(
            normalize(&Value::Character(b'A')),
            normalize(&Value::Character(b'A'))
        );
        assert_ne!(
            normalize(&Value::Character(b'A')),
            normalize(&Value::Character(b'B'))
        );
    }

    /// A primary present on only one side still carries tags, and the report
    /// documents `tag_presence` as "how many records on each side carry each tag
    /// at all". Skipping its tags would under-count the present side and hide a
    /// wholly one-sided tag behind an empty pattern key.
    #[test]
    fn a_one_sided_primary_still_contributes_its_tags() {
        let both: &[(&[u8; 2], Value)] = &[(b"NM", Value::Int32(3))];
        let only_query: &[(&[u8; 2], Value)] =
            &[(b"NM", Value::Int32(3)), (b"MQ", Value::Int32(60))];
        // Query has R1+R2; baseline has R1 only, so the R2 query primary is unmatched.
        let c = census_for(
            vec![rec("t", R1, 100, both), rec("t", R2, 300, only_query)],
            vec![rec("t", R1, 100, both)],
        );
        assert_eq!(c.total_primaries, 2);
        // The unmatched record's tags reach per-side presence.
        assert_eq!(c.tag_presence["MQ"].query, 1);
        assert_eq!(c.tag_presence["MQ"].baseline, 0);
        assert_eq!(
            c.tag_presence["NM"].query, 2,
            "both query primaries carry NM"
        );
        assert_eq!(c.tag_presence["NM"].baseline, 1);
        // ...and are counted as one-sided rather than silently dropped.
        assert_eq!(c.tag_query_only["MQ"], 1);
        assert_eq!(c.tag_query_only["NM"], 1);
        // The unmatched read is core-discordant and its pattern names both tags,
        // not the empty key.
        assert_eq!(c.patterns_core_discordant.get("MQ|NM"), Some(&1));
        assert_eq!(c.patterns_core_discordant.get(""), None);
    }

    #[test]
    fn identical_tags_produce_no_divergence() {
        let tags: &[(&[u8; 2], Value)] = &[(b"NM", Value::Int32(3)), (b"XS", Value::Int32(21))];
        let c = census_for(
            vec![rec("t", R1, 100, tags), rec("t", R2, 300, tags)],
            vec![rec("t", R1, 100, tags), rec("t", R2, 300, tags)],
        );
        assert_eq!(c.total_primaries, 2);
        assert_eq!(c.core_concordant, 2);
        assert!(c.tag_value_diff.is_empty());
        assert_eq!(c.patterns_core_concordant.get(""), Some(&2));
    }

    /// Tag order differs on every real record (fg-labs inserts MQ mid-block),
    /// so ordering must never register as a difference.
    #[test]
    fn tag_order_is_not_a_difference() {
        let c = census_for(
            vec![rec(
                "t",
                R1,
                100,
                &[(b"NM", Value::Int32(3)), (b"XS", Value::Int32(21))],
            )],
            vec![rec(
                "t",
                R1,
                100,
                &[(b"XS", Value::Int32(21)), (b"NM", Value::Int32(3))],
            )],
        );
        assert!(c.tag_value_diff.is_empty());
        assert_eq!(c.patterns_core_concordant.get(""), Some(&1));
    }

    /// The #290 shape: identical placement, one tag differing in value only.
    #[test]
    fn value_difference_is_recorded_against_a_core_concordant_read() {
        let c = census_for(
            vec![rec("t", R1, 100, &[(b"XS", Value::Int32(21))])],
            vec![rec("t", R1, 100, &[(b"XS", Value::Int32(0))])],
        );
        assert_eq!(c.core_concordant, 1, "placement is identical");
        assert_eq!(c.tag_value_diff.get("XS"), Some(&1));
        assert_eq!(c.patterns_core_concordant.get("XS"), Some(&1));
    }

    /// Presence divergence is tallied separately from value divergence, because
    /// a whole-tag absence (MQ/HN vs upstream) is a categorically different
    /// finding from a handful of differing values.
    #[test]
    fn presence_difference_is_separate_from_value_difference() {
        let c = census_for(
            vec![rec(
                "t",
                R1,
                100,
                &[(b"NM", Value::Int32(3)), (b"MQ", Value::Int32(60))],
            )],
            vec![rec("t", R1, 100, &[(b"NM", Value::Int32(3))])],
        );
        assert_eq!(c.tag_query_only.get("MQ"), Some(&1));
        assert!(c.tag_value_diff.is_empty());
        assert_eq!(c.tag_presence["MQ"].query, 1);
        assert_eq!(c.tag_presence["MQ"].baseline, 0);
    }

    /// A read discordant on core fields must not land in the core-concordant
    /// histogram — that half is what the tier arithmetic is computed over.
    #[test]
    fn core_discordant_reads_are_bucketed_separately() {
        let c = census_for(
            vec![rec("t", R1, 100, &[(b"XS", Value::Int32(21))])],
            vec![rec("t", R1, 999, &[(b"XS", Value::Int32(0))])],
        );
        assert_eq!(c.core_concordant, 0);
        assert_eq!(c.patterns_core_concordant.get("XS"), None);
        assert_eq!(c.patterns_core_discordant.get("XS"), Some(&1));
    }
}
