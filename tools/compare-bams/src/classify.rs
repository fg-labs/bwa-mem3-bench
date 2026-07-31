//! Classify a pair of alignment records into the set of fields on which they differ.

use noodles_sam::alignment::record::data::field::Tag;
use noodles_sam::alignment::record_buf::data::field::Value;
use noodles_sam::alignment::record_buf::RecordBuf;
use serde::{Deserialize, Serialize};

use crate::config::CompareOptions;

/// One field-level difference between two records.
///
/// [`classify`] returns *every* difference it finds rather than the first, so
/// aux tags are peers of the core fields rather than a fallback consulted only
/// when the core fields agree. That ordering matters: `NM` differing is usually
/// a consequence of `CIGAR` differing, but `XS` differing while everything else
/// matches is an independent signal (fg-labs/bwa-mem3#290), and a first-match
/// ladder would hide it behind any core difference.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Discordance {
    MappedOnlyQuery,
    MappedOnlyBaseline,
    /// Different chromosome, different position, or both.
    PosDiff {
        query_ref: Option<usize>,
        query_pos: Option<i64>,
        baseline_ref: Option<usize>,
        baseline_pos: Option<i64>,
    },
    CigarDiff,
    MapqDiff {
        query: u8,
        baseline: u8,
    },
    FlagDiff {
        query: u16,
        baseline: u16,
    },
    SecondarySetDiff,
    /// Both records carry `tag`, but with different values.
    TagValueDiff {
        tag: String,
    },
    /// Only the query record carries `tag`.
    TagQueryOnly {
        tag: String,
    },
    /// Only the baseline record carries `tag`.
    TagBaselineOnly {
        tag: String,
    },
}

impl Discordance {
    /// The aux tag this difference concerns, if it is a tag difference.
    #[must_use]
    pub fn tag(&self) -> Option<&str> {
        match self {
            Self::TagValueDiff { tag }
            | Self::TagQueryOnly { tag }
            | Self::TagBaselineOnly { tag } => Some(tag),
            _ => None,
        }
    }
}

/// A tag value normalized so equality ignores its on-disk encoding.
///
/// BAM stores integers in the narrowest type that fits, so two files that agree
/// completely can still encode one value as `c` in one and `i` in the other.
/// Comparing `noodles`' `Value` variants directly would call that a difference.
/// Borrows the record's bytes rather than owning them: this runs on every tag of
/// every record, so at ~10 tags x two records x millions of reads an owned
/// `Vec<u8>` per string tag (`MD`/`XA`/`SA` are long) would be on the order of
/// 10^8 short-lived allocations per comparison.
#[derive(Debug, PartialEq)]
enum NormalizedValue<'a> {
    Int(i64),
    Float(f32),
    /// `Z` (printable string) and `H` (hex byte array) are kept apart even when
    /// their bytes match: they are distinct SAM types written with distinct BAM
    /// type codes, so equal bytes under different types are a real difference
    /// rather than an encoding artifact the way `c` vs `i` is.
    Str(&'a [u8]),
    Hex(&'a [u8]),
    /// `A` (character), kept apart from `Int` for the same reason: it is its own
    /// SAM type, not a one-byte integer, so `XX:A:A` and `XX:i:65` differ.
    Char(u8),
    /// `B` arrays, compared via their debug rendering. No `B` tag appears in
    /// this workload; this arm exists so an unexpected one is still compared.
    Other(String),
}

fn normalize(value: &Value) -> NormalizedValue<'_> {
    match value {
        Value::Int8(n) => NormalizedValue::Int(i64::from(*n)),
        Value::UInt8(n) => NormalizedValue::Int(i64::from(*n)),
        Value::Int16(n) => NormalizedValue::Int(i64::from(*n)),
        Value::UInt16(n) => NormalizedValue::Int(i64::from(*n)),
        Value::Int32(n) => NormalizedValue::Int(i64::from(*n)),
        Value::UInt32(n) => NormalizedValue::Int(i64::from(*n)),
        Value::Character(c) => NormalizedValue::Char(*c),
        Value::Float(f) => NormalizedValue::Float(*f),
        Value::String(s) => NormalizedValue::Str(s.as_ref()),
        Value::Hex(s) => NormalizedValue::Hex(s.as_ref()),
        Value::Array(array) => NormalizedValue::Other(format!("{array:?}")),
    }
}

fn tag_name(tag: Tag) -> String {
    String::from_utf8_lossy(tag.as_ref()).into_owned()
}

/// Every tag difference between two records, ignoring `opts.ignore_tags`.
///
/// Compares the union of tags present on either side, so a tag one aligner
/// emits and the other does not is a difference rather than an omission.
/// Presence and value differences are distinct variants because a whole-tag
/// absence is systematic (it hits every record) while a value difference is
/// usually sporadic — conflating them lets the former bury the latter.
///
/// The ignore list is applied by the *caller* ([`classify`]) when splitting
/// these into scored and unscored, not here, so that an excluded tag is still
/// counted in the report. That is what makes a mis-classified tag
/// self-diagnosing rather than silently invisible.
fn all_tag_diffs(query: &RecordBuf, baseline: &RecordBuf) -> Vec<Discordance> {
    let mut diffs = Vec::new();
    // Keyed on `Tag` (a `Copy` two-byte code), not on an owned name: this is the
    // hot path, and `tag_name` is called only for the tags that actually end up
    // in a `Discordance`.
    let q: Vec<(Tag, &Value)> = query.data().iter().collect();
    let b: Vec<(Tag, &Value)> = baseline.data().iter().collect();

    for (tag, q_value) in &q {
        match b.iter().find(|(other, _)| other == tag) {
            Some((_, b_value)) => {
                if normalize(q_value) != normalize(b_value) {
                    diffs.push(Discordance::TagValueDiff {
                        tag: tag_name(*tag),
                    });
                }
            }
            None => diffs.push(Discordance::TagQueryOnly {
                tag: tag_name(*tag),
            }),
        }
    }
    for (tag, _) in &b {
        if !q.iter().any(|(other, _)| other == tag) {
            diffs.push(Discordance::TagBaselineOnly {
                tag: tag_name(*tag),
            });
        }
    }

    diffs.sort_by(|a, b| a.tag().cmp(&b.tag()));
    diffs
}

/// The outcome of comparing one pair of records.
///
/// Splitting scored from unscored differences — rather than dropping the
/// unscored ones — is deliberate: `ignore_tags` decides what counts against
/// concordance, never what is visible. A tag wrongly placed on the ignore list
/// still shows up in the report's `by_tag` block, so the mistake is diagnosable
/// from the JSON instead of hidden behind an unexplained score.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Classification {
    /// Differences that count against `concordance_pct`.
    pub diffs: Vec<Discordance>,
    /// Tag differences excluded by `ignore_tags`: tallied, never scored.
    pub ignored_tag_diffs: Vec<Discordance>,
}

impl Classification {
    /// True when nothing scored differs — the read is concordant.
    #[must_use]
    pub fn is_concordant(&self) -> bool {
        self.diffs.is_empty()
    }

    /// A classification carrying exactly one scored difference and nothing else.
    #[must_use]
    pub fn only_diff(d: Discordance) -> Self {
        Self {
            diffs: vec![d],
            ignored_tag_diffs: Vec::new(),
        }
    }
}

/// Compare two records representing the same read in query and baseline BAMs.
///
/// Returns every field on which they differ. The result is concordant when
/// [`Classification::diffs`] is empty.
/// Placement is compared via reference id, alignment start, CIGAR, MAPQ (within
/// `opts.mapq_tolerance`) and the flag bits that affect placement; aux tags are
/// compared per [`all_tag_diffs`], split by `opts.ignore_tags`.
///
/// When the two disagree on whether the read mapped at all, that single
/// difference is returned alone: the records describe incomparable outcomes, so
/// enumerating their field differences would be noise rather than diagnosis.
/// Tags *are* compared when both records are unmapped, since both sides still
/// emit `AS`/`XS` there.
///
/// # Panics
///
/// Does not panic: all optional fields are handled via [`Option::map`] and
/// [`Option::unwrap_or`].
#[must_use]
pub fn classify(query: &RecordBuf, baseline: &RecordBuf, opts: &CompareOptions) -> Classification {
    let q_mapped = !query.flags().is_unmapped();
    let b_mapped = !baseline.flags().is_unmapped();
    match (q_mapped, b_mapped) {
        (true, false) => return Classification::only_diff(Discordance::MappedOnlyQuery),
        (false, true) => return Classification::only_diff(Discordance::MappedOnlyBaseline),
        (false, false) => return split_tag_diffs(Vec::new(), query, baseline, opts),
        (true, true) => {}
    }

    let mut diffs = Vec::new();

    let q_ref = query.reference_sequence_id();
    let b_ref = baseline.reference_sequence_id();
    let q_pos = query
        .alignment_start()
        .map(|p| i64::try_from(usize::from(p)).unwrap_or(i64::MAX));
    let b_pos = baseline
        .alignment_start()
        .map(|p| i64::try_from(usize::from(p)).unwrap_or(i64::MAX));
    if q_ref != b_ref || q_pos != b_pos {
        diffs.push(Discordance::PosDiff {
            query_ref: q_ref,
            query_pos: q_pos,
            baseline_ref: b_ref,
            baseline_pos: b_pos,
        });
    }

    if query.cigar().as_ref() != baseline.cigar().as_ref() {
        diffs.push(Discordance::CigarDiff);
    }

    let q_mapq = query.mapping_quality().map_or(0, u8::from);
    let b_mapq = baseline.mapping_quality().map_or(0, u8::from);
    if q_mapq.abs_diff(b_mapq) > opts.mapq_tolerance {
        diffs.push(Discordance::MapqDiff {
            query: q_mapq,
            baseline: b_mapq,
        });
    }

    let placement_mask = 0x10 | 0x20 | 0x40 | 0x80; // strand + mate strand + r1 + r2
    let q_flags = query.flags().bits() & placement_mask;
    let b_flags = baseline.flags().bits() & placement_mask;
    if q_flags != b_flags {
        diffs.push(Discordance::FlagDiff {
            query: q_flags,
            baseline: b_flags,
        });
    }

    split_tag_diffs(diffs, query, baseline, opts)
}

/// Attach the pair's tag differences to `core_diffs`, routing each to the scored
/// or ignored bucket according to `opts.ignore_tags`.
fn split_tag_diffs(
    core_diffs: Vec<Discordance>,
    query: &RecordBuf,
    baseline: &RecordBuf,
    opts: &CompareOptions,
) -> Classification {
    let mut out = Classification {
        diffs: core_diffs,
        ignored_tag_diffs: Vec::new(),
    };
    for d in all_tag_diffs(query, baseline) {
        let ignored = d.tag().is_some_and(|t| opts.ignore_tags.contains(t));
        if ignored {
            out.ignored_tag_diffs.push(d);
        } else {
            out.diffs.push(d);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use noodles_core::Position;
    use noodles_sam::alignment::record::{Flags, MappingQuality};
    use noodles_sam::alignment::record_buf::Data;

    fn opts(ignore: &[&str]) -> CompareOptions {
        CompareOptions {
            ignore_tags: ignore.iter().map(|s| (*s).to_string()).collect(),
            mapq_tolerance: 0,
        }
    }

    fn rec(flags: u16, pos: usize, mapq: u8, tags: &[(&[u8; 2], Value)]) -> RecordBuf {
        let data: Data = tags
            .iter()
            .map(|(t, v)| (Tag::new(t[0], t[1]), v.clone()))
            .collect::<Vec<_>>()
            .into_iter()
            .collect();
        RecordBuf::builder()
            .set_flags(Flags::from_bits_retain(flags))
            .set_reference_sequence_id(0)
            .set_alignment_start(Position::new(pos).unwrap())
            .set_mapping_quality(MappingQuality::new(mapq).unwrap())
            .set_data(data)
            .build()
    }

    const NM3: (&[u8; 2], Value) = (b"NM", Value::Int32(3));

    #[test]
    fn identical_records_are_concordant() {
        let a = rec(0x40, 100, 60, &[NM3]);
        assert!(classify(&a, &a, &opts(&[])).is_concordant());
    }

    #[test]
    fn tag_value_difference_is_reported_when_placement_matches() {
        let q = rec(0x40, 100, 60, &[(b"XS", Value::Int32(21))]);
        let b = rec(0x40, 100, 60, &[(b"XS", Value::Int32(0))]);
        assert_eq!(
            classify(&q, &b, &opts(&[])).diffs,
            vec![Discordance::TagValueDiff {
                tag: "XS".to_string()
            }]
        );
    }

    /// Integer encoding width is not a difference: BAM picks the narrowest type
    /// that fits, so the same value may be stored as `c` on one side and `i` on
    /// the other.
    #[test]
    fn integer_encoding_width_is_not_a_difference() {
        let q = rec(0x40, 100, 60, &[(b"NM", Value::Int8(3))]);
        let b = rec(0x40, 100, 60, &[(b"NM", Value::Int32(3))]);
        assert!(classify(&q, &b, &opts(&[])).is_concordant());
    }

    /// fg-labs inserts `MQ` mid-block on every record, so a comparison
    /// sensitive to tag order would flag 100% of reads.
    #[test]
    fn tag_order_is_not_a_difference() {
        let q = rec(0x40, 100, 60, &[NM3, (b"XS", Value::Int32(9))]);
        let b = rec(0x40, 100, 60, &[(b"XS", Value::Int32(9)), NM3]);
        assert!(classify(&q, &b, &opts(&[])).is_concordant());
    }

    #[test]
    fn ignored_tags_are_compared_neither_by_value_nor_by_presence() {
        let q = rec(0x40, 100, 60, &[NM3, (b"MQ", Value::Int32(60))]);
        let b = rec(0x40, 100, 60, &[(b"NM", Value::Int32(9))]);
        // MQ is query-only and NM differs; ignoring both leaves nothing.
        assert!(classify(&q, &b, &opts(&["MQ", "NM"])).is_concordant());
        // Ignoring only MQ still reports the NM value difference.
        assert_eq!(
            classify(&q, &b, &opts(&["MQ"])).diffs,
            vec![Discordance::TagValueDiff {
                tag: "NM".to_string()
            }]
        );
    }

    #[test]
    fn one_sided_tags_are_presence_differences_not_value_differences() {
        let q = rec(0x40, 100, 60, &[NM3, (b"HN", Value::Int32(1))]);
        let b = rec(0x40, 100, 60, &[NM3, (b"YD", Value::Int32(1))]);
        assert_eq!(
            classify(&q, &b, &opts(&[])).diffs,
            vec![
                Discordance::TagQueryOnly {
                    tag: "HN".to_string()
                },
                Discordance::TagBaselineOnly {
                    tag: "YD".to_string()
                },
            ]
        );
    }

    /// Every difference is returned, not just the first — otherwise a tag
    /// difference would be masked by any core-field difference on the same read.
    /// Collapsing integer *width* is deliberate (see above); collapsing the
    /// *type* is not. `Z` and `H` are distinct SAM types written with distinct
    /// BAM type codes, so identical bytes under the two types are a real on-disk
    /// difference rather than an encoding artifact the way `c` vs `i` is.
    #[test]
    fn string_and_hex_with_the_same_bytes_are_a_difference() {
        let q = rec(0x40, 100, 60, &[(b"XX", Value::String("7F".into()))]);
        let b = rec(0x40, 100, 60, &[(b"XX", Value::Hex("7F".into()))]);
        let diffs = classify(&q, &b, &opts(&[])).diffs;
        assert_eq!(diffs.len(), 1, "{diffs:?}");
        assert_eq!(diffs[0].tag(), Some("XX"));
    }

    /// Same rule as `Z` vs `H`: `A` is its own SAM type, not a one-byte integer.
    /// Folding it into `Int` would make `XX:A:A` and `XX:i:65` compare equal.
    #[test]
    fn character_and_integer_with_the_same_code_point_are_a_difference() {
        let q = rec(0x40, 100, 60, &[(b"XX", Value::Character(b'A'))]);
        let b = rec(0x40, 100, 60, &[(b"XX", Value::Int32(i32::from(b'A')))]);
        let diffs = classify(&q, &b, &opts(&[])).diffs;
        assert_eq!(diffs.len(), 1, "{diffs:?}");
        assert_eq!(diffs[0].tag(), Some("XX"));
    }

    #[test]
    fn core_and_tag_differences_are_both_reported() {
        let q = rec(0x40, 100, 60, &[(b"XS", Value::Int32(21))]);
        let b = rec(0x40, 999, 60, &[(b"XS", Value::Int32(0))]);
        let diffs = classify(&q, &b, &opts(&[])).diffs;
        assert_eq!(diffs.len(), 2, "{diffs:?}");
        assert!(matches!(diffs[0], Discordance::PosDiff { .. }));
        assert_eq!(diffs[1].tag(), Some("XS"));
    }

    #[test]
    fn mapping_disagreement_short_circuits_to_a_single_finding() {
        let mapped = rec(0x40, 100, 60, &[(b"XS", Value::Int32(21))]);
        let unmapped = rec(0x40 | 0x4, 100, 0, &[(b"XS", Value::Int32(0))]);
        assert_eq!(
            classify(&mapped, &unmapped, &opts(&[])).diffs,
            vec![Discordance::MappedOnlyQuery]
        );
        assert_eq!(
            classify(&unmapped, &mapped, &opts(&[])).diffs,
            vec![Discordance::MappedOnlyBaseline]
        );
    }

    /// Both-unmapped pairs still carry AS/XS, so their tags are compared.
    #[test]
    fn both_unmapped_still_compares_tags() {
        let q = rec(0x4, 100, 0, &[(b"AS", Value::Int32(0))]);
        let b = rec(0x4, 100, 0, &[(b"AS", Value::Int32(5))]);
        assert_eq!(
            classify(&q, &b, &opts(&[])).diffs,
            vec![Discordance::TagValueDiff {
                tag: "AS".to_string()
            }]
        );
        assert!(classify(&q, &q, &opts(&[])).is_concordant());
    }

    #[test]
    fn mapq_tolerance_is_respected() {
        let q = rec(0x40, 100, 60, &[]);
        let b = rec(0x40, 100, 57, &[]);
        let mut tolerant = opts(&[]);
        tolerant.mapq_tolerance = 3;
        assert!(classify(&q, &b, &tolerant).is_concordant());
        assert_eq!(
            classify(&q, &b, &opts(&[])).diffs,
            vec![Discordance::MapqDiff {
                query: 60,
                baseline: 57
            }]
        );
    }
}
