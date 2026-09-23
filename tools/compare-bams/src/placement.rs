//! Confident-placement agreement: do the two aligners put the reads that at
//! least one of them is SURE about in the same place?
//!
//! `concordance_pct` answers a different question — is every scored field of
//! every primary identical — and against a *different aligner* that question
//! is dominated by things that carry no placement information. Measured on
//! `meth-twist-emseq-5M` (bwa-mem3 v0.13.0 against bwameth, 10.37M primaries):
//! the 27.77% headline drift was 24.1 pp of `XS` (the suboptimal score, which
//! describes the candidate set, not the chosen hit), and of the 155,463
//! position differences 97% were reads where NEITHER aligner reached MAPQ 20 —
//! two aligners picking different copies of a repeat. Neither is a finding.
//!
//! This axis keeps the part that is: among primaries where either side has
//! MAPQ >= `confident_mapq`, how many land on a different contig or strand,
//! more than `relocation_bp` apart, or unmapped on the other side. It is split
//! by which side is confident, because the one-sided groups are where a real
//! regression would surface — a build that became confidently wrong shows up in
//! `query_only` long before it moves the reads both sides agree on.
//!
//! Distance is measured between UNCLIPPED 5' ends, not `POS`. On the same
//! measurement 43% of confident position differences had an identical unclipped
//! 5' end: the same placement with a different soft-clip, which `POS` alone
//! reports as a move.
//!
//! Reported for every comparison kind; which gate reads it, and against what
//! budget, is decided downstream (`docs/expected-divergences.yaml`).

use std::collections::BTreeMap;

use noodles_sam::alignment::record::cigar::op::Kind;
use noodles_sam::alignment::record_buf::RecordBuf;
use serde::{Deserialize, Serialize};

/// Which side(s) reached the confidence threshold for one read end.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfidentSide {
    Both,
    QueryOnly,
    BaselineOnly,
}

impl ConfidentSide {
    /// Key under [`PlacementReport::by_group`].
    fn key(self) -> &'static str {
        match self {
            Self::Both => "both",
            Self::QueryOnly => "query_only",
            Self::BaselineOnly => "baseline_only",
        }
    }
}

/// How one confident read end compares across the two sides.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlacementOutcome {
    /// Same contig, strand, and unclipped 5' end.
    Same,
    /// Same contig and strand, unclipped 5' ends `1..=relocation_bp` apart.
    Shifted,
    /// Different contig or strand, more than `relocation_bp` apart, or
    /// unmapped (or absent) on the other side.
    Relocated,
}

/// Per-group read counts.
#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GroupCounts {
    pub reads: u64,
    pub shifted: u64,
    pub relocated: u64,
}

/// The `placement` block of the comparison report.
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct PlacementReport {
    /// MAPQ at or above which a side counts as confident.
    pub confident_mapq: u8,
    /// Largest unclipped-5'-end distance still counted as the same locus.
    pub relocation_bp: u32,
    /// Primaries where at least one side is confident (the denominator).
    pub confident_reads: u64,
    /// Confident primaries shifted by `1..=relocation_bp` — reported, not scored.
    pub shifted: u64,
    /// Confident primaries that moved (see [`PlacementOutcome::Relocated`]).
    pub relocated: u64,
    /// `relocated / confident_reads`, in percent; `None` (JSON `null`) when
    /// there were no confident reads. 0 of 0 is not 0% relocated, and a gate
    /// reading 0.0 there would pass a cell that measured nothing.
    pub relocated_pct: Option<f64>,
    /// The same counts split by which side was confident: `both`,
    /// `query_only`, `baseline_only`. Every key is always present.
    pub by_group: BTreeMap<String, GroupCounts>,
}

impl PlacementReport {
    /// An empty report carrying the thresholds it will be measured under.
    #[must_use]
    pub fn new(confident_mapq: u8, relocation_bp: u32) -> Self {
        let by_group = [
            ConfidentSide::Both,
            ConfidentSide::QueryOnly,
            ConfidentSide::BaselineOnly,
        ]
        .into_iter()
        .map(|g| (g.key().to_string(), GroupCounts::default()))
        .collect();
        Self {
            confident_mapq,
            relocation_bp,
            by_group,
            ..Default::default()
        }
    }

    /// Tally one read end. Either side may be absent (no primary for this end),
    /// which is treated as unmapped.
    pub fn record(&mut self, query: Option<&RecordBuf>, baseline: Option<&RecordBuf>) {
        let Some((side, outcome)) =
            assess(query, baseline, self.confident_mapq, self.relocation_bp)
        else {
            return;
        };
        self.confident_reads += 1;
        let group = self.by_group.entry(side.key().to_string()).or_default();
        group.reads += 1;
        match outcome {
            PlacementOutcome::Same => {}
            PlacementOutcome::Shifted => {
                self.shifted += 1;
                group.shifted += 1;
            }
            PlacementOutcome::Relocated => {
                self.relocated += 1;
                group.relocated += 1;
            }
        }
    }

    /// Compute `relocated_pct`. Call once after every read end has been recorded.
    pub fn finalize(&mut self) {
        #[allow(clippy::cast_precision_loss)]
        let pct = (self.confident_reads > 0)
            .then(|| self.relocated as f64 / self.confident_reads as f64 * 100.0);
        self.relocated_pct = pct;
    }
}

/// Classify one read end, or `None` when neither side is confident.
#[must_use]
pub fn assess(
    query: Option<&RecordBuf>,
    baseline: Option<&RecordBuf>,
    confident_mapq: u8,
    relocation_bp: u32,
) -> Option<(ConfidentSide, PlacementOutcome)> {
    let query = query.filter(|r| !r.flags().is_unmapped());
    let baseline = baseline.filter(|r| !r.flags().is_unmapped());
    let is_confident = |r: Option<&RecordBuf>| {
        r.is_some_and(|r| r.mapping_quality().map_or(0, u8::from) >= confident_mapq)
    };
    let side = match (is_confident(query), is_confident(baseline)) {
        (true, true) => ConfidentSide::Both,
        (true, false) => ConfidentSide::QueryOnly,
        (false, true) => ConfidentSide::BaselineOnly,
        (false, false) => return None,
    };
    let (Some(q), Some(b)) = (query, baseline) else {
        return Some((side, PlacementOutcome::Relocated));
    };
    if q.reference_sequence_id() != b.reference_sequence_id()
        || q.flags().is_reverse_complemented() != b.flags().is_reverse_complemented()
    {
        return Some((side, PlacementOutcome::Relocated));
    }
    let outcome = match (unclipped_five_prime(q), unclipped_five_prime(b)) {
        (Some(qp), Some(bp)) => match qp.abs_diff(bp) {
            0 => PlacementOutcome::Same,
            d if d <= u64::from(relocation_bp) => PlacementOutcome::Shifted,
            _ => PlacementOutcome::Relocated,
        },
        // A mapped record with no position is malformed; count it as a move
        // rather than silently as agreement.
        _ => PlacementOutcome::Relocated,
    };
    Some((side, outcome))
}

/// 1-based reference coordinate of a mapped record's unclipped 5' end: the
/// leftmost unclipped base on the forward strand, the rightmost on the reverse.
///
/// Signed because clipping can extend past the start of the contig.
#[must_use]
pub fn unclipped_five_prime(record: &RecordBuf) -> Option<i64> {
    let start = i64::try_from(usize::from(record.alignment_start()?)).ok()?;
    let ops = record.cigar().as_ref();
    let is_clip = |k: Kind| matches!(k, Kind::SoftClip | Kind::HardClip);
    let clip_len = |ops: &mut dyn Iterator<Item = &noodles_sam::alignment::record::cigar::Op>| {
        ops.take_while(|op| is_clip(op.kind()))
            .map(|op| op.len())
            .sum::<usize>()
    };
    if record.flags().is_reverse_complemented() {
        let span: usize = ops
            .iter()
            .filter(|op| op.kind().consumes_reference())
            .map(|op| op.len())
            .sum();
        let trailing = clip_len(&mut ops.iter().rev());
        let end = start + i64::try_from(span).ok()? - 1;
        Some(end + i64::try_from(trailing).ok()?)
    } else {
        let leading = clip_len(&mut ops.iter());
        Some(start - i64::try_from(leading).ok()?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use noodles_core::Position;
    use noodles_sam::alignment::record::cigar::Op;
    use noodles_sam::alignment::record::{Flags, MappingQuality};
    use noodles_sam::alignment::record_buf::Cigar;

    const REVERSE: u16 = 0x10;
    const UNMAPPED: u16 = 0x4;

    fn rec(flags: u16, ref_id: usize, pos: usize, mapq: u8, cigar: &[(Kind, usize)]) -> RecordBuf {
        let cigar: Cigar = cigar.iter().map(|&(k, n)| Op::new(k, n)).collect();
        RecordBuf::builder()
            .set_flags(Flags::from_bits_retain(flags))
            .set_reference_sequence_id(ref_id)
            .set_alignment_start(Position::new(pos).unwrap())
            .set_mapping_quality(MappingQuality::new(mapq).unwrap())
            .set_cigar(cigar)
            .build()
    }

    fn m(n: usize) -> (Kind, usize) {
        (Kind::Match, n)
    }
    fn s(n: usize) -> (Kind, usize) {
        (Kind::SoftClip, n)
    }

    #[test]
    fn unclipped_five_prime_forward_subtracts_leading_clip() {
        let r = rec(0, 0, 100, 60, &[s(5), m(70)]);
        assert_eq!(unclipped_five_prime(&r), Some(95));
    }

    #[test]
    fn unclipped_five_prime_reverse_is_the_rightmost_base_plus_trailing_clip() {
        // 100 + 70 - 1 = 169 is the last aligned base; 3 clipped bases follow it.
        let r = rec(
            REVERSE,
            0,
            100,
            60,
            &[s(5), m(40), (Kind::Deletion, 2), m(28), s(3)],
        );
        assert_eq!(unclipped_five_prime(&r), Some(172));
    }

    #[test]
    fn a_soft_clip_difference_at_the_same_locus_is_not_a_move() {
        // POS differs by 5, but the unclipped 5' ends coincide.
        let q = rec(0, 0, 100, 60, &[m(75)]);
        let b = rec(0, 0, 105, 60, &[s(5), m(70)]);
        assert_eq!(
            assess(Some(&q), Some(&b), 20, 10),
            Some((ConfidentSide::Both, PlacementOutcome::Same))
        );
    }

    #[test]
    fn distance_threshold_is_inclusive() {
        let q = rec(0, 0, 100, 60, &[m(75)]);
        let at = rec(0, 0, 110, 60, &[m(75)]);
        let past = rec(0, 0, 111, 60, &[m(75)]);
        assert_eq!(
            assess(Some(&q), Some(&at), 20, 10).unwrap().1,
            PlacementOutcome::Shifted
        );
        assert_eq!(
            assess(Some(&q), Some(&past), 20, 10).unwrap().1,
            PlacementOutcome::Relocated
        );
    }

    #[test]
    fn different_contig_or_strand_is_relocated() {
        let q = rec(0, 0, 100, 60, &[m(75)]);
        let other_contig = rec(0, 1, 100, 60, &[m(75)]);
        let other_strand = rec(REVERSE, 0, 26, 60, &[m(75)]); // 5' end at 100
        assert_eq!(
            assess(Some(&q), Some(&other_contig), 20, 10).unwrap().1,
            PlacementOutcome::Relocated
        );
        assert_eq!(
            assess(Some(&q), Some(&other_strand), 20, 10).unwrap().1,
            PlacementOutcome::Relocated
        );
    }

    #[test]
    fn neither_side_confident_is_not_counted() {
        let q = rec(0, 0, 100, 19, &[m(75)]);
        let b = rec(0, 1, 9_000, 0, &[m(75)]);
        assert_eq!(assess(Some(&q), Some(&b), 20, 10), None);
    }

    #[test]
    fn one_sided_confidence_is_attributed_to_that_side() {
        let q = rec(0, 0, 100, 60, &[m(75)]);
        let b = rec(0, 1, 9_000, 3, &[m(75)]);
        assert_eq!(
            assess(Some(&q), Some(&b), 20, 10),
            Some((ConfidentSide::QueryOnly, PlacementOutcome::Relocated))
        );
        assert_eq!(
            assess(Some(&b), Some(&q), 20, 10),
            Some((ConfidentSide::BaselineOnly, PlacementOutcome::Relocated))
        );
    }

    #[test]
    fn confident_read_unmapped_or_absent_on_the_other_side_is_relocated() {
        let q = rec(0, 0, 100, 60, &[m(75)]);
        let unmapped = rec(UNMAPPED, 0, 100, 0, &[]);
        assert_eq!(
            assess(Some(&q), Some(&unmapped), 20, 10),
            Some((ConfidentSide::QueryOnly, PlacementOutcome::Relocated))
        );
        assert_eq!(
            assess(None, Some(&q), 20, 10),
            Some((ConfidentSide::BaselineOnly, PlacementOutcome::Relocated))
        );
    }

    #[test]
    fn no_confident_reads_serializes_the_percentage_as_null() {
        // 0 of 0 is not 0% relocated: a gate reading 0.0 here would pass a cell
        // that measured nothing.
        let mut report = PlacementReport::new(20, 10);
        let low = rec(0, 0, 500, 0, &[m(75)]);
        report.record(Some(&low), Some(&low));
        report.finalize();
        assert_eq!(report.confident_reads, 0);
        assert_eq!(report.relocated_pct, None);
        let json = serde_json::to_value(&report).unwrap();
        assert!(json["relocated_pct"].is_null());
    }

    #[test]
    fn report_counts_and_percentage() {
        let mut report = PlacementReport::new(20, 10);
        let here = rec(0, 0, 100, 60, &[m(75)]);
        let near = rec(0, 0, 104, 60, &[m(75)]);
        let far = rec(0, 2, 100, 60, &[m(75)]);
        let low = rec(0, 0, 500, 0, &[m(75)]);
        report.record(Some(&here), Some(&here)); // both, same
        report.record(Some(&here), Some(&near)); // both, shifted
        report.record(Some(&here), Some(&far)); // both, relocated
        report.record(Some(&low), Some(&far)); // baseline_only, relocated
        report.record(Some(&low), Some(&low)); // neither: not counted
        report.finalize();
        assert_eq!(report.confident_reads, 4);
        assert_eq!(report.shifted, 1);
        assert_eq!(report.relocated, 2);
        assert!((report.relocated_pct.unwrap() - 50.0).abs() < 1e-9);
        assert_eq!(
            report.by_group["both"],
            GroupCounts {
                reads: 3,
                shifted: 1,
                relocated: 1
            }
        );
        assert_eq!(
            report.by_group["baseline_only"],
            GroupCounts {
                reads: 1,
                shifted: 0,
                relocated: 1
            }
        );
        assert_eq!(report.by_group["query_only"], GroupCounts::default());
    }
}
