//! Template-grouped comparison of two BAMs over the same FASTQ input.
//!
//! For each template (read pair) the two BAMs are compared on two axes:
//!   * **primary concordance** — each read end's primary alignment is classified
//!     via [`classify`] (position / CIGAR / MAPQ / placement flags, plus every
//!     aux tag not excluded by `ignore_tags`); this drives the headline
//!     `concordance_pct`.
//!   * **supplementary disagreement** — supplementary alignments are counted and
//!     position-matched separately, since `bwa-mem3` legitimately emits a
//!     different number of them than `bwa-mem2 v2.2.1` (non-bit-identical
//!     output). These never fail the comparison; they are reported as metrics.

use std::collections::HashMap;
use std::io::Read;

use anyhow::Result;
use noodles_sam::alignment::record_buf::RecordBuf;

use crate::classify::{classify, Classification, Discordance};
use crate::config::CompareOptions;
use crate::report::ConcordanceReport;
use crate::template_reader::{template_iter, Template};

/// Stream two BAMs grouped by template and return an aggregated report.
///
/// # Errors
///
/// Returns an error if a BAM header cannot be read, the two BAMs diverge in
/// template identity / order, or an underlying read fails.
pub fn compare<R1, R2>(query: R1, baseline: R2, opts: &CompareOptions) -> Result<ConcordanceReport>
where
    R1: Read,
    R2: Read,
{
    let mut report = ConcordanceReport::default();
    for template in template_iter(query, baseline)? {
        compare_template(&template?, opts, &mut report);
    }
    report.finalize();
    Ok(report)
}

/// True for a primary alignment record (neither secondary nor supplementary).
fn is_primary(r: &RecordBuf) -> bool {
    let f = r.flags();
    !f.is_secondary() && !f.is_supplementary()
}

/// Key distinguishing read ends within a template: 1 = first, 2 = last,
/// 3 = both bits set, 0 = unpaired. Lets us pair query/baseline primaries by end
/// without assuming a fixed record order.
fn end_key(r: &RecordBuf) -> u8 {
    let f = r.flags();
    u8::from(f.is_first_segment()) | (u8::from(f.is_last_segment()) << 1)
}

/// Position key for matching supplementary alignments across the two BAMs.
fn supp_key(r: &RecordBuf) -> (Option<usize>, Option<i64>, bool) {
    let pos = r
        .alignment_start()
        .map(|p| i64::try_from(usize::from(p)).unwrap_or(i64::MAX));
    (
        r.reference_sequence_id(),
        pos,
        r.flags().is_reverse_complemented(),
    )
}

/// Number of supplementary records that do not have a position-matched
/// counterpart on the other side (multiset symmetric difference over
/// `(ref_id, start, strand)`).
fn supp_unmatched(query: &[&RecordBuf], baseline: &[&RecordBuf]) -> u64 {
    let mut counts: HashMap<(Option<usize>, Option<i64>, bool), i64> = HashMap::new();
    for r in query {
        *counts.entry(supp_key(r)).or_default() += 1;
    }
    for r in baseline {
        *counts.entry(supp_key(r)).or_default() -= 1;
    }
    counts.values().map(|v| v.unsigned_abs()).sum()
}

/// Compare one template: classify each end's primary and tally supplementaries.
fn compare_template(t: &Template, opts: &CompareOptions, report: &mut ConcordanceReport) {
    let mut q_primary: HashMap<u8, &RecordBuf> = HashMap::new();
    let mut b_primary: HashMap<u8, &RecordBuf> = HashMap::new();
    let mut q_supp: Vec<&RecordBuf> = Vec::new();
    let mut b_supp: Vec<&RecordBuf> = Vec::new();

    for r in &t.query {
        if r.flags().is_supplementary() {
            q_supp.push(r);
        } else if is_primary(r) {
            q_primary.insert(end_key(r), r);
        }
    }
    for r in &t.baseline {
        if r.flags().is_supplementary() {
            b_supp.push(r);
        } else if is_primary(r) {
            b_primary.insert(end_key(r), r);
        }
    }

    // Primary concordance, one classified unit per read end present on either side.
    let mut ends: Vec<u8> = q_primary.keys().chain(b_primary.keys()).copied().collect();
    ends.sort_unstable();
    ends.dedup();
    for end in ends {
        let classification = match (q_primary.get(&end), b_primary.get(&end)) {
            (Some(q), Some(b)) => classify(q, b, opts),
            (Some(_), None) => Classification::only_diff(Discordance::MappedOnlyQuery),
            (None, Some(_)) => Classification::only_diff(Discordance::MappedOnlyBaseline),
            (None, None) => continue,
        };
        report.record(&classification);
    }

    // Supplementary disagreement axis.
    report.record_supplementary(
        q_supp.len() as u64,
        b_supp.len() as u64,
        supp_unmatched(&q_supp, &b_supp),
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use noodles_core::Position;
    use noodles_sam::alignment::record::{Flags, MappingQuality};

    const R1: u16 = 0x40; // FIRST_SEGMENT
    const R2: u16 = 0x80; // LAST_SEGMENT
    const SUPP: u16 = 0x800;

    fn rec(name: &str, flags: u16, ref_id: usize, pos: usize, mapq: u8) -> RecordBuf {
        RecordBuf::builder()
            .set_name(name)
            .set_flags(Flags::from_bits_retain(flags))
            .set_reference_sequence_id(ref_id)
            .set_alignment_start(Position::new(pos).unwrap())
            .set_mapping_quality(MappingQuality::new(mapq).unwrap())
            .build()
    }

    fn report_for(query: Vec<RecordBuf>, baseline: Vec<RecordBuf>) -> ConcordanceReport {
        let t = Template {
            name: "t".to_string(),
            query,
            baseline,
        };
        let mut report = ConcordanceReport::default();
        compare_template(&t, &CompareOptions::default(), &mut report);
        report.finalize();
        report
    }

    #[test]
    fn identical_primaries_no_supps_are_concordant() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R2, 0, 300, 60)],
            vec![rec("t", R1, 0, 100, 60), rec("t", R2, 0, 300, 60)],
        );
        assert_eq!(r.total_reads, 2);
        assert_eq!(r.concordant, 2);
        assert_eq!(r.total_templates, 1);
        assert_eq!(r.supp_count_mismatch_templates, 0);
        assert_eq!(r.supp_unmatched, 0);
    }

    #[test]
    fn extra_query_supplementary_is_counted_not_fatal() {
        // Same primaries; query has one extra supplementary the baseline lacks.
        let r = report_for(
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R2, 0, 300, 60),
                rec("t", R1 | SUPP, 1, 5000, 0),
            ],
            vec![rec("t", R1, 0, 100, 60), rec("t", R2, 0, 300, 60)],
        );
        assert_eq!(r.concordant, 2); // primaries still match
        assert_eq!(r.supp_query_total, 1);
        assert_eq!(r.supp_baseline_total, 0);
        assert_eq!(r.supp_count_mismatch_templates, 1);
        assert_eq!(r.supp_unmatched, 1);
    }

    #[test]
    fn moved_supplementary_same_count_but_unmatched_by_position() {
        let r = report_for(
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R2, 0, 300, 60),
                rec("t", R1 | SUPP, 1, 5000, 0),
            ],
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R2, 0, 300, 60),
                rec("t", R1 | SUPP, 2, 9999, 0), // same count, different locus
            ],
        );
        assert_eq!(r.supp_count_mismatch_templates, 0); // counts equal
        assert_eq!(r.supp_unmatched, 2); // one unmatched on each side
    }

    #[test]
    fn primary_position_difference_is_discordant() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R2, 0, 300, 60)],
            vec![rec("t", R1, 0, 999, 60), rec("t", R2, 0, 300, 60)],
        );
        assert_eq!(r.total_reads, 2);
        assert_eq!(r.concordant, 1);
        assert_eq!(r.by_class.get("pos_diff").map(|c| c.count), Some(1));
    }
}
