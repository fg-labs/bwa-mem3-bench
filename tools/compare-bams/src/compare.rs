//! Template-grouped comparison of two BAMs over the same FASTQ input.
//!
//! For each template (read pair) the two BAMs are compared on three axes:
//!   * **primary concordance** — each read end's primary alignment is classified
//!     via [`classify`] (position / CIGAR / MAPQ / the whole FLAG, plus every
//!     aux tag not excluded by `ignore_tags`); this drives the headline
//!     `concordance_pct`.
//!   * **supplementary divergence** — supplementary alignments are paired on
//!     `(end, ref_id, start, strand)`; pairs that match are compared on their
//!     full content, and records with no counterpart are counted. Both are
//!     reported as their own metrics and never enter `concordance_pct`, since
//!     `bwa-mem3` legitimately emits a different number of them than
//!     `bwa-mem2 v2.2.1`.
//!   * **secondary divergence** — the same treatment for `0x100` records, which
//!     reached neither of the other two axes before and were scored by nothing.

use std::collections::{BTreeSet, HashMap, VecDeque};
use std::io::Read;

use anyhow::Result;
use noodles_sam::alignment::record_buf::RecordBuf;

use crate::classify::{classify, Classification, Discordance};
use crate::config::CompareOptions;
use crate::report::{discordance_key, ConcordanceReport, NonPrimaryClass, NonPrimaryTally};
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

    // Flag every ignored tag in `by_tag`, including ones that never diverged or
    // never appeared, so the JSON states the whole policy rather than only the
    // part of it that happened to fire.
    for tag in &opts.ignore_tags {
        report.mark_ignored(tag);
    }

    if opts.tag_guard {
        report.tag_guard_violations = crate::guard::check(&report, opts);
    }
    Ok(report)
}

/// Key distinguishing read ends within a template: 1 = first, 2 = last,
/// 3 = both bits set, 0 = unpaired. Lets us pair query/baseline primaries by end
/// without assuming a fixed record order.
fn end_key(r: &RecordBuf) -> u8 {
    let f = r.flags();
    u8::from(f.is_first_segment()) | (u8::from(f.is_last_segment()) << 1)
}

/// Identity key pairing a non-primary record with its counterpart.
///
/// `end_key` is part of the key, not an afterthought: two non-primary records of
/// one template can sit at the SAME `(ref_id, start, strand)` on different ends —
/// routine on Hi-C, where `hic-1M` carries 381,418 supplementaries in 2.38M
/// records. Keyed on position alone they cross-pair, and the content comparison
/// below then manufactures a FLAG difference on `0x40`/`0x80` plus a CIGAR
/// difference out of two files that agree perfectly.
type NonPrimaryKey = (u8, Option<usize>, Option<i64>, bool);

fn non_primary_key(r: &RecordBuf) -> NonPrimaryKey {
    let pos = r
        .alignment_start()
        .map(|p| i64::try_from(usize::from(p)).unwrap_or(i64::MAX));
    (
        end_key(r),
        r.reference_sequence_id(),
        pos,
        r.flags().is_reverse_complemented(),
    )
}

/// Pair one class of non-primary records across the two sides, compare the
/// content of every matched pair, and count the rest.
///
/// Pairing is by [`NonPrimaryKey`], which deliberately excludes CIGAR, MAPQ and
/// tags: putting them in the key would turn a difference in any of them into an
/// *unmatched* record, hiding exactly what this function exists to surface.
///
/// The cost of that choice is that two records of one template can share a key,
/// leaving nothing but content to tell them apart — so within such a bucket a
/// query record takes a CONCORDANT counterpart if one is there, and falls back
/// to encounter order only when none is. Encounter order alone is deterministic
/// but not correct: two files that agree, listing the two records in opposite
/// order, cross-pair under it and report a content difference on both. Pinned by
/// `duplicate_key_non_primaries_in_reversed_order_are_not_content_diffs`.
///
/// This cannot hide a real difference. It only ever chooses BETWEEN counterparts
/// already in the bucket, and a query record with no concordant counterpart
/// still pairs and still reports its diffs — see
/// `a_real_difference_in_a_duplicate_key_bucket_still_counts`. Both `matched` and
/// `unmatched` are unaffected: the same records pair either way, only which pairs
/// with which changes.
///
/// The scan is quadratic in the size of one bucket, which is a non-issue at the
/// sizes that occur: a bucket holds the records of ONE template that share an
/// end, reference, start and strand, so it is almost always a single record.
fn compare_non_primary(
    query: &[&RecordBuf],
    baseline: &[&RecordBuf],
    opts: &CompareOptions,
) -> NonPrimaryTally {
    let mut buckets: HashMap<NonPrimaryKey, VecDeque<&RecordBuf>> = HashMap::new();
    for r in baseline {
        buckets.entry(non_primary_key(r)).or_default().push_back(r);
    }

    let mut tally = NonPrimaryTally {
        query_total: query.len() as u64,
        baseline_total: baseline.len() as u64,
        ..Default::default()
    };

    for q in query {
        let Some(bucket) = buckets.get_mut(&non_primary_key(q)) else {
            tally.unmatched += 1;
            continue;
        };
        // A concordant counterpart if the bucket holds one, else the first
        // record in it. `None` from both means the bucket is empty: every
        // baseline record with this key has already been paired.
        let concordant = bucket
            .iter()
            .position(|b| classify(q, b, opts).is_concordant());
        let chosen = concordant.or_else(|| (!bucket.is_empty()).then_some(0));
        let Some(b) = chosen.and_then(|i| bucket.remove(i)) else {
            tally.unmatched += 1;
            continue;
        };
        tally.matched += 1;
        if concordant.is_some() {
            // Already classified as concordant while searching; nothing to add,
            // and re-classifying would only cost another pass over the tags.
            continue;
        }
        // Discordant by construction — the search above found no concordant
        // record in this bucket — but re-checked rather than assumed, so a later
        // change to the selection cannot silently start counting concordant
        // pairs as differences.
        let classification = classify(q, b, opts);
        if !classification.is_concordant() {
            tally.content_diffs += 1;
            // Same class keys as the primary axis, but accumulated here rather
            // than through `ConcordanceReport::record` — that would put
            // non-primary records into `total_reads` and `by_class`, both
            // documented primary-only.
            let mut seen: BTreeSet<&'static str> = BTreeSet::new();
            for d in &classification.diffs {
                seen.insert(discordance_key(d));
            }
            for key in seen {
                *tally.by_class.entry(key.to_string()).or_default() += 1;
            }
        }
    }
    // Whatever is left in the buckets had no query counterpart.
    tally.unmatched += buckets.values().map(|v| v.len() as u64).sum::<u64>();
    tally
}

/// Compare one template: classify each end's primary and tally supplementaries.
fn compare_template(t: &Template, opts: &CompareOptions, report: &mut ConcordanceReport) {
    let mut q_primary: HashMap<u8, &RecordBuf> = HashMap::new();
    let mut b_primary: HashMap<u8, &RecordBuf> = HashMap::new();
    let mut q_supp: Vec<&RecordBuf> = Vec::new();
    let mut b_supp: Vec<&RecordBuf> = Vec::new();
    let mut q_sec: Vec<&RecordBuf> = Vec::new();
    let mut b_sec: Vec<&RecordBuf> = Vec::new();

    // Every record is ROUTED to exactly one bucket. The chain this replaced
    // tested `is_supplementary()` then `is_primary()` and had no arm for a record
    // that is neither, so a SECONDARY alignment fell out of it entirely — scored
    // by nothing, in this crate or anywhere downstream.
    //
    // Routed, not necessarily retained: the primary map is keyed by end, so a
    // second primary for an end already seen replaces the first. Pre-existing,
    // and correct for conforming input (one primary per end), but it does mean a
    // malformed BAM drops records here silently.
    for (side, primary, supp, sec) in [
        (&t.query, &mut q_primary, &mut q_supp, &mut q_sec),
        (&t.baseline, &mut b_primary, &mut b_supp, &mut b_sec),
    ] {
        for r in side {
            let flags = r.flags();
            if flags.is_supplementary() {
                supp.push(r);
            } else if flags.is_secondary() {
                sec.push(r);
            } else {
                primary.insert(end_key(r), r);
            }
        }
    }

    // Primary concordance, one classified unit per read end present on either side.
    let mut ends: Vec<u8> = q_primary.keys().chain(b_primary.keys()).copied().collect();
    ends.sort_unstable();
    ends.dedup();
    for end in ends {
        let (q, b) = (q_primary.get(&end), b_primary.get(&end));
        let classification = match (q, b) {
            (Some(q), Some(b)) => classify(q, b, opts),
            (Some(_), None) => Classification::only_diff(Discordance::MappedOnlyQuery),
            (None, Some(_)) => Classification::only_diff(Discordance::MappedOnlyBaseline),
            (None, None) => continue,
        };
        report.record(&classification);
    }

    // Presence is censused across EVERY record on both sides, not just the
    // classified primaries. `by_tag` is documented as a census of every tag
    // *observed*, and [`crate::guard`] reads it: a tag carried only by a
    // secondary or supplementary would otherwise never be seen, so the guard
    // would wave through an unexpected tag and would call a live `ignore_tags`
    // entry dead. Scoring stays primary-only -- this loop only tallies presence.
    for record in &t.query {
        report.record_presence(Some(record), None);
    }
    for record in &t.baseline {
        report.record_presence(None, Some(record));
    }

    // Non-primary axes. Reported separately from `concordance_pct` / `by_class`,
    // which stay primary-only so the headline number remains comparable across
    // aligner versions that legitimately emit different numbers of these.
    report.record_non_primary(
        NonPrimaryClass::Supplementary,
        &compare_non_primary(&q_supp, &b_supp, opts),
    );
    report.record_non_primary(
        NonPrimaryClass::Secondary,
        &compare_non_primary(&q_sec, &b_sec, opts),
    );
    report.count_template();
}

#[cfg(test)]
mod tests {
    use super::*;
    use noodles_core::Position;
    use noodles_sam::alignment::record::data::field::Tag;
    use noodles_sam::alignment::record::{Flags, MappingQuality};
    use noodles_sam::alignment::record_buf::data::field::Value;
    use noodles_sam::alignment::record_buf::Data;

    const R1: u16 = 0x40; // FIRST_SEGMENT
    const R2: u16 = 0x80; // LAST_SEGMENT
    const SUPP: u16 = 0x800;
    const SEC: u16 = 0x100;

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

    /// `by_tag` is documented as a census of every tag *observed*, and the guard
    /// reads it to tell an unexpected tag from a dead `ignore_tags` entry. A tag
    /// carried only by a supplementary must therefore reach it: censusing only
    /// the classified primaries would let an unanticipated tag through the guard
    /// entirely, and would call a live ignore entry dead.
    #[test]
    fn a_tag_only_on_a_supplementary_is_still_censused() {
        let mut supp = rec("t", R1 | SUPP, 0, 500, 60);
        let data: Data = [(Tag::new(b'X', b'Y'), Value::Int32(1))]
            .into_iter()
            .collect();
        *supp.data_mut() = data;
        let report = report_for(
            vec![rec("t", R1, 0, 100, 60), supp],
            vec![rec("t", R1, 0, 100, 60)],
        );
        let counter = report
            .by_tag
            .get("XY")
            .expect("a supplementary-only tag must still be observed");
        assert_eq!(counter.query_present, 1);
        assert_eq!(counter.baseline_present, 0);
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

    /// A secondary record used to fall out of the routing chain entirely: not
    /// `is_supplementary()`, not `is_primary()`, so it reached neither bucket
    /// and was scored by nothing. Live on the meth samples today, where
    /// `--meth` implicitly sets `MEM_F_NO_MULTI` and split hits are re-flagged
    /// secondary rather than supplementary.
    #[test]
    fn a_secondary_record_is_tallied_not_dropped() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SEC, 1, 5000, 0)],
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SEC, 1, 5000, 0)],
        );
        assert_eq!(r.sec_query_total, 1);
        assert_eq!(r.sec_baseline_total, 1);
        assert_eq!(r.sec_unmatched, 0);
        assert_eq!(r.sec_matched, 1);
    }

    #[test]
    fn an_extra_query_secondary_is_counted_unmatched() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SEC, 1, 5000, 0)],
            vec![rec("t", R1, 0, 100, 60)],
        );
        assert_eq!(r.sec_query_total, 1);
        assert_eq!(r.sec_baseline_total, 0);
        assert_eq!(r.sec_unmatched, 1);
        assert_eq!(r.sec_count_mismatch_templates, 1);
    }

    /// Supplementaries that position-match were never compared on anything but
    /// position: their FLAG, CIGAR, MAPQ and tags went unread. Measured cost on
    /// one real ALT cell: 1 FLAG and 567 `pa` differences invisible.
    #[test]
    fn matched_supplementaries_differing_in_mapq_are_content_diffs() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SUPP, 1, 5000, 60)],
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SUPP, 1, 5000, 11)],
        );
        assert_eq!(r.supp_unmatched, 0, "same locus, so they pair");
        assert_eq!(r.supp_matched, 1);
        assert_eq!(r.supp_content_diffs, 1, "MAPQ differs on the matched pair");
    }

    /// Pairing on `(ref_id, start, strand)` alone cross-pairs two
    /// supplementaries of one template that sit at the same locus on different
    /// ends — routine on Hi-C. The key must carry `end_key` too, or the pairing
    /// manufactures a FLAG difference on 0x40/0x80 and a CIGAR difference.
    #[test]
    fn supplementaries_at_one_locus_on_different_ends_do_not_cross_pair() {
        // The two sides list the ends in OPPOSITE order on purpose. Listed in the
        // same order, encounter-order pairing lands on the right counterpart even
        // without `end_key` in the key, and the test passes whether or not the
        // contract it names holds.
        let r = report_for(
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R1 | SUPP, 1, 5000, 60),
                rec("t", R2 | SUPP, 1, 5000, 60),
            ],
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R2 | SUPP, 1, 5000, 60),
                rec("t", R1 | SUPP, 1, 5000, 60),
            ],
        );
        assert_eq!(r.supp_matched, 2);
        assert_eq!(r.supp_unmatched, 0);
        assert_eq!(
            r.supp_content_diffs, 0,
            "each end must pair with its own counterpart, not the other end's"
        );
    }

    /// Two records sharing a key are only distinguishable by content, so pairing
    /// them in encounter order manufactures differences out of two files that
    /// agree: list them in opposite order and every pair is cross-matched. The
    /// key deliberately excludes CIGAR/MAPQ/tags — putting them in would turn a
    /// real difference into an unmatched record — so the bucket must resolve
    /// order by preferring a concordant counterpart.
    #[test]
    fn duplicate_key_non_primaries_in_reversed_order_are_not_content_diffs() {
        let r = report_for(
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R1 | SUPP, 1, 5000, 60),
                rec("t", R1 | SUPP, 1, 5000, 11),
            ],
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R1 | SUPP, 1, 5000, 11),
                rec("t", R1 | SUPP, 1, 5000, 60),
            ],
        );
        assert_eq!(r.supp_matched, 2);
        assert_eq!(r.supp_unmatched, 0);
        assert_eq!(
            r.supp_content_diffs, 0,
            "both records have an identical counterpart; order must not invent a difference"
        );
    }

    /// The other half of the contract: preferring a concordant counterpart must
    /// not let a genuine difference disappear. One record matches exactly, the
    /// other does not, so exactly one content diff must survive.
    #[test]
    fn a_real_difference_in_a_duplicate_key_bucket_still_counts() {
        let r = report_for(
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R1 | SUPP, 1, 5000, 60),
                rec("t", R1 | SUPP, 1, 5000, 11),
            ],
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R1 | SUPP, 1, 5000, 60),
                rec("t", R1 | SUPP, 1, 5000, 42),
            ],
        );
        assert_eq!(r.supp_matched, 2);
        assert_eq!(r.supp_unmatched, 0);
        assert_eq!(
            r.supp_content_diffs, 1,
            "the unmatched-content pair must still be reported"
        );
        assert_eq!(r.supp_by_class.get("mapq_diff").copied(), Some(1));
    }

    /// `end_key` in the pairing key CHANGED what `unmatched` counts, and that
    /// metric is persisted in `benchmark.db`. Under the old
    /// `(ref_id, start, strand)` key these two records cancelled and reported 0;
    /// they are different ends, so they now correctly report 2. Pinned because
    /// the change is silent in the JSON and invisible to every other test.
    #[test]
    fn non_primaries_on_different_ends_at_one_locus_no_longer_cancel() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SUPP, 1, 5000, 60)],
            vec![rec("t", R2, 0, 100, 60), rec("t", R2 | SUPP, 1, 5000, 60)],
        );
        assert_eq!(r.supp_matched, 0, "different ends must not pair");
        assert_eq!(r.supp_unmatched, 2, "one unmatched on each side");
    }

    /// A bare `content_diffs` count is not diagnosable: on a real ALT cell it
    /// read 568 with no way to tell 1 FLAG difference from 567 `pa` differences
    /// without re-deriving it outside the tool. Each axis therefore carries its
    /// own class breakdown, kept separate from the primary-only `by_class`.
    #[test]
    fn non_primary_content_diffs_are_broken_down_by_class() {
        let r = report_for(
            vec![
                rec("t", R1, 0, 100, 60),
                rec("t", R1 | SUPP, 1, 5000, 60),
                rec("t", R2 | SUPP, 2, 7000, 60),
            ],
            vec![
                rec("t", R1, 0, 100, 60),
                // same locus, different MAPQ -> mapq_diff
                rec("t", R1 | SUPP, 1, 5000, 11),
                // same locus, different FLAG (mate-reverse) -> flag_diff
                rec("t", R2 | SUPP | 0x20, 2, 7000, 60),
            ],
        );
        assert_eq!(r.supp_content_diffs, 2);
        assert_eq!(r.supp_by_class.get("mapq_diff").copied(), Some(1));
        assert_eq!(r.supp_by_class.get("flag_diff").copied(), Some(1));
        assert!(
            r.by_class.is_empty(),
            "primary by_class must stay untouched"
        );
    }

    /// The Secondary arm of `record_non_primary`'s field routing is otherwise
    /// unasserted: every other non-primary test exercises the Supplementary arm,
    /// so swapping the two content pointers would be invisible.
    #[test]
    fn a_secondary_content_difference_is_reported_on_the_secondary_axis() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SEC, 1, 5000, 60)],
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SEC, 1, 5000, 11)],
        );
        assert_eq!(r.sec_matched, 1);
        assert_eq!(
            r.sec_content_diffs, 1,
            "MAPQ differs on the matched secondary"
        );
        assert_eq!(
            r.supp_content_diffs, 0,
            "must not land on the supplementary axis"
        );
    }

    /// `concordance_pct` and `by_class` are documented as primary-only so the
    /// headline number stays comparable across releases. Non-primary content
    /// comparison must not leak into either, nor into `total_reads` — which
    /// `guard.rs` renders as `"N of {total_reads} primaries"`.
    #[test]
    fn non_primary_content_diffs_stay_out_of_the_primary_score() {
        let r = report_for(
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SUPP, 1, 5000, 60)],
            vec![rec("t", R1, 0, 100, 60), rec("t", R1 | SUPP, 1, 5000, 11)],
        );
        assert_eq!(r.total_reads, 1, "one primary end, not two records");
        assert_eq!(r.concordant, 1);
        assert!((r.concordance_pct - 100.0).abs() < 1e-9);
        assert!(r.by_class.is_empty(), "by_class is primary-only");
        assert_eq!(
            r.supp_content_diffs, 1,
            "but the difference is still reported"
        );
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
