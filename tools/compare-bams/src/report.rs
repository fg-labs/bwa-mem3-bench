//! Aggregated comparison report, serializable to JSON.

use std::collections::{BTreeMap, BTreeSet};

use noodles_sam::alignment::record_buf::RecordBuf;
use serde::{Deserialize, Serialize};

use crate::classify::{tag_str, Classification, Discordance};
use crate::guard::TagGuardViolation;

/// Per-class count and percentage bucket in a [`ConcordanceReport`].
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ClassCounter {
    pub count: u64,
    pub pct: f64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub samples: Vec<String>,
}

/// Per-tag divergence detail, split by how the two records disagreed.
///
/// Presence and value are counted separately because they are different
/// findings: a tag one aligner never writes diverges on every record, while a
/// value difference is usually sporadic. Reported together, the former buries
/// the latter.
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct TagCounter {
    /// Both records carried the tag; the values differed.
    pub value_diff: u64,
    /// Only the query record carried the tag.
    pub query_only: u64,
    /// Only the baseline record carried the tag.
    pub baseline_only: u64,
    /// Records on the query side carrying this tag at all, whether or not it
    /// diverged. Together with `baseline_present` this makes `by_tag` a census
    /// of every tag *observed* rather than only of those that differed, which
    /// is what lets [`crate::guard`] tell an unexpected tag from a dead
    /// `ignore_tags` entry — and lets the error message quantify both.
    #[serde(default)]
    pub query_present: u64,
    /// Records on the baseline side carrying this tag at all.
    #[serde(default)]
    pub baseline_present: u64,
    /// True when this tag is on the `ignore_tags` list, so any divergence it has
    /// is reported here but excluded from `concordance_pct`. Set for every
    /// ignored tag, including ones that never diverged or never appeared at all,
    /// so the block states the whole policy rather than the part that fired.
    /// Present so a reader can tell "diverges and we accepted it" from "diverges
    /// and it counted".
    #[serde(default)]
    pub ignored: bool,
}

impl TagCounter {
    /// Whether this tag appeared on at least one record of either side.
    ///
    /// The meaning of the two presence counters, and the predicate both of
    /// [`crate::guard`]'s checks turn on — an entry can exist with all-zero
    /// counts (see [`ConcordanceReport::mark_ignored`]), so "has an entry" and
    /// "was observed" are different questions.
    #[must_use]
    pub fn observed(&self) -> bool {
        self.query_present > 0 || self.baseline_present > 0
    }
}

/// Aggregated result of comparing two BAMs, suitable for JSON serialization.
///
/// `concordance_pct` and `by_class` cover **primary** alignments only (one unit
/// per read end), so the headline number stays comparable across aligner
/// versions even when non-primary emission differs. The `supp_*` and `sec_*`
/// fields report supplementary and secondary divergence as separate, non-fatal
/// axes — each carrying totals, unmatched records, matched pairs and the subset
/// of matched pairs whose content differs. All fields are additive over the
/// historical schema, so existing JSON consumers keep working; `ingest.py`'s
/// `_SUPP_KEYS` whitelist must be widened in step with them or a new field is
/// silently dropped before it reaches `benchmark.db`.
///
/// A read is concordant only when it differs in **no** compared field —
/// placement or aux tag. Because one read can differ in several fields at once,
/// `by_class` counts *reads exhibiting each class* and therefore sums to at
/// least (and usually more than) the number of discordant reads. Only
/// `concordance_pct` is a partition; `by_class` is a set of overlapping
/// populations.
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ConcordanceReport {
    /// Total primary alignment records compared (denominator of `concordance_pct`).
    pub total_reads: u64,
    pub concordant: u64,
    pub concordance_pct: f64,
    pub by_class: BTreeMap<String, ClassCounter>,

    /// Per-tag census, keyed by tag name. Holds an entry for EVERY tag observed
    /// on either side — not only those that diverged (see [`Self::record_presence`])
    /// — plus every `ignore_tags` entry (see [`Self::mark_ignored`]), which may
    /// have all-zero counts when it matched no record. Ignored tags carry
    /// `ignored: true` and are excluded from `concordance_pct` but never hidden,
    /// so a misclassified tag is diagnosable from this block alone.
    #[serde(default)]
    pub by_tag: BTreeMap<String, TagCounter>,

    /// Templates (read pairs) seen.
    pub total_templates: u64,
    /// Supplementary records in the query / baseline BAMs respectively.
    pub supp_query_total: u64,
    pub supp_baseline_total: u64,
    /// Templates where the two BAMs carry a different supplementary count.
    pub supp_count_mismatch_templates: u64,
    pub supp_count_mismatch_pct: f64,
    /// Supplementary records lacking a position-matched counterpart on the other
    /// side (union over both BAMs).
    pub supp_unmatched: u64,
    pub supp_unmatched_pct: f64,
    /// Supplementary pairs that DID match on `(end, ref_id, start, strand)`, and
    /// how many of those differ once their content is compared.
    ///
    /// Position-matching alone never read a supplementary's FLAG, CIGAR, MAPQ or
    /// tags, so a pair could sit at the same locus and still disagree on every
    /// other field. Measured on one real ALT-aware cell: 1 FLAG and 567 `pa`
    /// differences rode supplementary records and were invisible.
    #[serde(default)]
    pub supp_matched: u64,
    #[serde(default)]
    pub supp_content_diffs: u64,
    /// `supp_content_diffs` split by class — see `NonPrimaryTally::by_class`.
    #[serde(default)]
    pub supp_by_class: BTreeMap<String, u64>,

    /// The secondary-alignment axis, mirroring `supp_*` field for field.
    ///
    /// Secondary records reached NEITHER the primary map nor the supplementary
    /// tally before this existed — the routing chain tested `is_supplementary()`
    /// then `is_primary()`, and a secondary is neither. Live on the meth samples,
    /// where `--meth` implicitly sets `MEM_F_NO_MULTI` and split hits are
    /// re-flagged secondary rather than supplementary.
    #[serde(default)]
    pub sec_query_total: u64,
    #[serde(default)]
    pub sec_baseline_total: u64,
    #[serde(default)]
    pub sec_count_mismatch_templates: u64,
    #[serde(default)]
    pub sec_count_mismatch_pct: f64,
    #[serde(default)]
    pub sec_unmatched: u64,
    #[serde(default)]
    pub sec_unmatched_pct: f64,
    #[serde(default)]
    pub sec_matched: u64,
    #[serde(default)]
    pub sec_content_diffs: u64,
    /// `sec_content_diffs` split by class — see `NonPrimaryTally::by_class`.
    #[serde(default)]
    pub sec_by_class: BTreeMap<String, u64>,

    /// Ways the observed tag set deviated from what the config declared. Empty
    /// on a healthy run, and omitted from the JSON when empty so the schema is
    /// unchanged for every passing comparison.
    ///
    /// The verdict lives in the report as well as on stderr, because the run
    /// that trips the guard exits non-zero *after* writing this file. Note the
    /// limit of that: under the workflow this JSON is the compare rule's
    /// `output:`, and snakemake deletes the outputs of a failed job, so the
    /// file survives only for a direct/manual invocation. In the Batch pipeline
    /// the verdict reaches an operator through the worker's `CloudWatch` stderr,
    /// which is why [`crate::guard::TagGuardViolation::message`] is written to
    /// stand alone rather than to annotate this block.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tag_guard_violations: Vec<TagGuardViolation>,
}

/// Which non-primary alignment class a tally belongs to.
///
/// The two are reported as separate axes rather than merged: they mean
/// different things (a split hit versus an alternative placement), the
/// divergence registry's `affected` field already distinguishes
/// `supplementary_alignment` from `secondary_alignment`, and merging them
/// would hide a class appearing where it never did before.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NonPrimaryClass {
    Supplementary,
    Secondary,
}

/// One template's tally for a single non-primary class.
#[derive(Debug, Default)]
pub struct NonPrimaryTally {
    pub query_total: u64,
    pub baseline_total: u64,
    /// Pairs that matched on `(end, ref_id, start, strand)`.
    pub matched: u64,
    /// Matched pairs whose CONTENT differs — the axis position-matching alone
    /// could never see, since it never read FLAG, CIGAR, MAPQ or tags.
    pub content_diffs: u64,
    /// `content_diffs` split by discordance class, same keys as the primary
    /// `by_class`. A bare count is not diagnosable: on a real ALT-aware cell it
    /// read 568 with no way to tell 1 FLAG difference from 567 `pa` tag
    /// differences without re-deriving the comparison outside the tool. Like
    /// `by_class`, a pair contributes at most once per class but may appear in
    /// several, so this sums to at least `content_diffs`.
    pub by_class: BTreeMap<String, u64>,
    /// Records with no counterpart on the other side, summed over both BAMs.
    ///
    /// **The definition changed when the pairing key gained the read end.** The
    /// multiset symmetric difference this replaced keyed on
    /// `(ref_id, start, strand)`, so two non-primaries of one template at the
    /// same locus on DIFFERENT ends cancelled and reported 0; keyed on
    /// `(end, ref_id, start, strand)` they no longer pair and report 2. The new
    /// answer is the correct one — they are different reads — but the metric is
    /// persisted in `benchmark.db`, so values either side of this boundary are
    /// not comparable and a jump in `supp_unmatched` across it is expected
    /// rather than a regression. Pinned by
    /// `non_primaries_on_different_ends_at_one_locus_no_longer_cancel`.
    pub unmatched: u64,
}

impl ConcordanceReport {
    /// Record one classified primary read.
    ///
    /// A read with no *scored* differences is concordant; ignored tag
    /// differences are tallied in `by_tag` without affecting that. A read
    /// contributes at most once to each `by_class` bucket no matter how many
    /// differences of that class it carries — five differing tags on one read
    /// is one `tag_diff` read, not five.
    pub fn record(&mut self, c: &Classification) {
        self.total_reads += 1;

        // Ignored tag differences are tallied but never affect the score or the
        // class buckets, so a tag on the ignore list stays visible in `by_tag`.
        for d in &c.ignored_tag_diffs {
            self.tally_tag(d, true);
        }

        if c.is_concordant() {
            self.concordant += 1;
            return;
        }

        let mut classes: BTreeSet<&'static str> = BTreeSet::new();
        for d in &c.diffs {
            classes.insert(discordance_key(d));
            self.tally_tag(d, false);
        }
        for key in classes {
            self.by_class.entry(key.to_string()).or_default().count += 1;
        }
    }

    /// Record which tags each side of one compared read carried.
    ///
    /// Kept separate from [`Self::record`] rather than folded into it: `record`
    /// takes only a [`Classification`], which is what makes the report's own
    /// tests pure, and presence is the one thing that cannot be derived from a
    /// classification — [`crate::classify::classify`] reports tags that
    /// *differ*, so a tag present and identical on both sides never appears
    /// there. Either side may be `None` when only one side has a record to offer.
    ///
    /// Called for EVERY record on both sides — secondaries and supplementaries
    /// included — not only the classified primaries, so `by_tag` really is the
    /// census of observed tags that [`crate::guard`] relies on. A tag emitted
    /// only on a supplementary would otherwise be invisible to the guard, which
    /// would then wave through an unexpected tag and call a live `ignore_tags`
    /// entry dead. Scoring is unaffected: `record` still sees primaries only, so
    /// these counts deliberately do NOT share `total_reads`' denominator.
    pub fn record_presence(&mut self, query: Option<&RecordBuf>, baseline: Option<&RecordBuf>) {
        for (record, is_query) in [(query, true), (baseline, false)] {
            let Some(record) = record else { continue };
            for (tag, _) in record.data().iter() {
                // Borrow the two tag bytes as &str rather than allocating a
                // String per tag per record: this runs on every tag of every
                // read, where the diff path runs only on the ones that differ.
                // BTreeMap<String, _> looks up by &str, so the only allocation
                // is on first sight of a tag.
                //
                // Measured on panel-twist-5M (7.9M primaries, ~9 tags/side, so
                // ~140M lookups) against the same binary with this call stubbed
                // out, interleaved A/B over a warm page cache: 72.3s without vs
                // 75.8s with, i.e. under 5% — and the two distributions overlap
                // (75.8s worst without, 68.6s best with), so the effect is not
                // separable from run-to-run noise at n=4. Not worth a
                // hand-rolled tag-interning structure.
                let Some(name) = tag_str(&tag) else { continue };
                let entry = match self.by_tag.get_mut(name) {
                    Some(entry) => entry,
                    None => self.by_tag.entry(name.to_string()).or_default(),
                };
                if is_query {
                    entry.query_present += 1;
                } else {
                    entry.baseline_present += 1;
                }
            }
        }
    }

    /// Mark `tag` as excluded from the score, creating its `by_tag` entry if
    /// this comparison never observed it.
    ///
    /// Needed because `ignored` is otherwise only set when a tag *diverges*, so
    /// a correctly-ignored tag that happens to agree everywhere — or one absent
    /// entirely — would read as un-ignored in the JSON.
    pub fn mark_ignored(&mut self, tag: &str) {
        self.by_tag.entry(tag.to_string()).or_default().ignored = true;
    }

    /// Add one tag difference to `by_tag`; a no-op for non-tag differences.
    fn tally_tag(&mut self, d: &Discordance, ignored: bool) {
        let Some(tag) = d.tag() else { return };
        let entry = self.by_tag.entry(tag.to_string()).or_default();
        entry.ignored = ignored;
        match d {
            Discordance::TagValueDiff { .. } => entry.value_diff += 1,
            Discordance::TagQueryOnly { .. } => entry.query_only += 1,
            Discordance::TagBaselineOnly { .. } => entry.baseline_only += 1,
            _ => {}
        }
    }

    /// Count one template. Kept separate from [`Self::record_non_primary`],
    /// which is now called once per class per template and must not multiply
    /// the template count by the number of classes.
    pub fn count_template(&mut self) {
        self.total_templates += 1;
    }

    /// Record one template's tally for a single non-primary class.
    ///
    /// Deliberately NOT routed through [`Self::record`]: that increments
    /// `total_reads`, `concordant` and `by_class`, and calls `tally_tag`. Feeding
    /// non-primary records to it would decouple `by_tag`'s diff counts from
    /// `total_reads`' denominator and make `guard`'s `"N of {total_reads}
    /// primaries"` message false. `concordance_pct` and `by_class` stay
    /// primary-only, as this type's docstring promises.
    pub fn record_non_primary(&mut self, class: NonPrimaryClass, tally: &NonPrimaryTally) {
        let (query_total, baseline_total, mismatch, unmatched, matched, content, by_class) =
            match class {
                NonPrimaryClass::Supplementary => (
                    &mut self.supp_query_total,
                    &mut self.supp_baseline_total,
                    &mut self.supp_count_mismatch_templates,
                    &mut self.supp_unmatched,
                    &mut self.supp_matched,
                    &mut self.supp_content_diffs,
                    &mut self.supp_by_class,
                ),
                NonPrimaryClass::Secondary => (
                    &mut self.sec_query_total,
                    &mut self.sec_baseline_total,
                    &mut self.sec_count_mismatch_templates,
                    &mut self.sec_unmatched,
                    &mut self.sec_matched,
                    &mut self.sec_content_diffs,
                    &mut self.sec_by_class,
                ),
            };
        for (key, count) in &tally.by_class {
            *by_class.entry(key.clone()).or_default() += count;
        }
        *query_total += tally.query_total;
        *baseline_total += tally.baseline_total;
        if tally.query_total != tally.baseline_total {
            *mismatch += 1;
        }
        *unmatched += tally.unmatched;
        *matched += tally.matched;
        *content += tally.content_diffs;
    }

    /// Compute derived percentages. Call once after all records have been fed via [`Self::record`].
    pub fn finalize(&mut self) {
        #[allow(clippy::cast_precision_loss)]
        if self.total_reads > 0 {
            let total = self.total_reads as f64;
            self.concordance_pct = self.concordant as f64 / total * 100.0;
            for v in self.by_class.values_mut() {
                v.pct = v.count as f64 / total * 100.0;
            }
        }

        #[allow(clippy::cast_precision_loss)]
        if self.total_templates > 0 {
            let templates = self.total_templates as f64;
            self.supp_count_mismatch_pct =
                self.supp_count_mismatch_templates as f64 / templates * 100.0;
            self.sec_count_mismatch_pct =
                self.sec_count_mismatch_templates as f64 / templates * 100.0;
        }

        let supp_total = self.supp_query_total + self.supp_baseline_total;
        #[allow(clippy::cast_precision_loss)]
        if supp_total > 0 {
            self.supp_unmatched_pct = self.supp_unmatched as f64 / supp_total as f64 * 100.0;
        }

        let sec_total = self.sec_query_total + self.sec_baseline_total;
        #[allow(clippy::cast_precision_loss)]
        if sec_total > 0 {
            self.sec_unmatched_pct = self.sec_unmatched as f64 / sec_total as f64 * 100.0;
        }
    }

    /// Pretty-print the report as JSON.
    ///
    /// # Errors
    ///
    /// Returns an error if the report cannot be serialized. For the current type
    /// definition this only fails if a future field is added with a `Serialize`
    /// impl that can fail (e.g. due to invalid UTF-8 in a map key); today this is
    /// effectively infallible.
    pub fn to_json(&self) -> serde_json::Result<String> {
        serde_json::to_string_pretty(self)
    }
}

/// The `by_class` bucket a difference belongs to.
///
/// All three tag variants collapse to a single `tag_diff` bucket so the class
/// list stays fixed-cardinality for the report tables; the per-tag breakdown
/// lives in `by_tag`.
pub(crate) fn discordance_key(d: &Discordance) -> &'static str {
    match d {
        Discordance::MappedOnlyQuery => "mapped_only_query",
        Discordance::MappedOnlyBaseline => "mapped_only_baseline",
        Discordance::PosDiff { .. } => "pos_diff",
        Discordance::CigarDiff => "cigar_diff",
        Discordance::MapqDiff { .. } => "mapq_diff",
        Discordance::FlagDiff { .. } => "flag_diff",
        Discordance::TagValueDiff { .. }
        | Discordance::TagQueryOnly { .. }
        | Discordance::TagBaselineOnly { .. } => "tag_diff",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tag_value(tag: &str) -> Discordance {
        Discordance::TagValueDiff {
            tag: tag.to_string(),
        }
    }

    /// A record carrying `tags`, for exercising the presence path.
    fn with_tags(tags: &[(&str, i32)]) -> RecordBuf {
        use noodles_sam::alignment::record::data::field::Tag;
        use noodles_sam::alignment::record_buf::data::field::Value;
        use noodles_sam::alignment::record_buf::Data;
        let data: Data = tags
            .iter()
            .map(|(name, v)| {
                let b = name.as_bytes();
                (Tag::new(b[0], b[1]), Value::Int32(*v))
            })
            .collect::<Vec<_>>()
            .into_iter()
            .collect();
        RecordBuf::builder().set_data(data).build()
    }

    /// A read whose only scored differences are `diffs`, with no ignored tags.
    fn scored(diffs: Vec<Discordance>) -> Classification {
        Classification {
            diffs,
            ignored_tag_diffs: Vec::new(),
        }
    }

    #[test]
    fn a_read_with_no_differences_is_concordant() {
        let mut r = ConcordanceReport::default();
        r.record(&scored(vec![]));
        r.finalize();
        assert_eq!((r.total_reads, r.concordant), (1, 1));
        assert!((r.concordance_pct - 100.0).abs() < f64::EPSILON);
        assert!(r.by_class.is_empty());
    }

    /// One read differing in several tags is one discordant read and one
    /// `tag_diff`, but each tag is tallied individually in `by_tag`.
    #[test]
    fn multiple_tag_diffs_on_one_read_count_once_as_a_class() {
        let mut r = ConcordanceReport::default();
        r.record(&scored(vec![
            tag_value("NM"),
            tag_value("MD"),
            tag_value("XS"),
        ]));
        r.finalize();
        assert_eq!(r.concordant, 0);
        assert_eq!(r.by_class["tag_diff"].count, 1);
        for tag in ["NM", "MD", "XS"] {
            assert_eq!(r.by_tag[tag].value_diff, 1);
            assert!(!r.by_tag[tag].ignored);
        }
    }

    /// A read differing in both placement and tags appears in both buckets, so
    /// `by_class` counts overlapping populations rather than a partition.
    #[test]
    fn by_class_buckets_overlap_across_a_single_read() {
        let mut r = ConcordanceReport::default();
        r.record(&scored(vec![Discordance::CigarDiff, tag_value("NM")]));
        r.finalize();
        assert_eq!(r.total_reads, 1);
        assert_eq!(r.concordant, 0);
        assert_eq!(r.by_class["cigar_diff"].count, 1);
        assert_eq!(r.by_class["tag_diff"].count, 1);
        let summed: u64 = r.by_class.values().map(|c| c.count).sum();
        assert_eq!(summed, 2, "two buckets over one discordant read");
    }

    #[test]
    fn presence_and_value_differences_are_tallied_separately() {
        let mut r = ConcordanceReport::default();
        r.record(&scored(vec![Discordance::TagQueryOnly {
            tag: "MQ".to_string(),
        }]));
        r.record(&scored(vec![Discordance::TagBaselineOnly {
            tag: "YD".to_string(),
        }]));
        r.record(&scored(vec![tag_value("MQ")]));
        r.finalize();
        assert_eq!(r.by_tag["MQ"].query_only, 1);
        assert_eq!(r.by_tag["MQ"].value_diff, 1);
        assert_eq!(r.by_tag["YD"].baseline_only, 1);
        assert_eq!(r.by_class["tag_diff"].count, 3);
    }

    /// The point of keeping ignored differences: a tag on the ignore list still
    /// appears in `by_tag` (flagged `ignored`) so a misclassification is
    /// diagnosable, but it must not touch the score or the class buckets.
    #[test]
    fn ignored_tag_diffs_are_visible_but_never_scored() {
        let mut r = ConcordanceReport::default();
        r.record(&Classification {
            diffs: vec![],
            ignored_tag_diffs: vec![tag_value("NM"), tag_value("MD")],
        });
        r.finalize();
        assert_eq!(r.concordant, 1, "ignored tags leave the read concordant");
        assert!((r.concordance_pct - 100.0).abs() < f64::EPSILON);
        assert!(r.by_class.is_empty(), "no tag_diff bucket for ignored tags");
        assert_eq!(r.by_tag["NM"].value_diff, 1);
        assert!(r.by_tag["NM"].ignored);
        assert!(r.by_tag["MD"].ignored);
    }

    /// An ignored tag alongside a real difference: the read is discordant on the
    /// real one only, and both tags remain visible with the right `ignored` flag.
    #[test]
    fn ignored_and_scored_tags_coexist_on_one_read() {
        let mut r = ConcordanceReport::default();
        r.record(&Classification {
            diffs: vec![tag_value("XS")],
            ignored_tag_diffs: vec![tag_value("NM")],
        });
        r.finalize();
        assert_eq!(r.concordant, 0);
        assert_eq!(r.by_class["tag_diff"].count, 1);
        assert!(!r.by_tag["XS"].ignored);
        assert!(r.by_tag["NM"].ignored);
    }

    /// Presence is what the guard reads, and it is exactly the thing a
    /// classification cannot supply: a tag present and identical on both sides
    /// produces no `Discordance`, so without this it would be invisible.
    #[test]
    fn presence_is_counted_for_tags_that_never_diverge() {
        let mut r = ConcordanceReport::default();
        let rec = with_tags(&[("NM", 3), ("MQ", 60)]);
        r.record(&scored(vec![]));
        r.record_presence(Some(&rec), Some(&rec));
        r.finalize();
        assert_eq!(r.concordant, 1, "identical tags leave the read concordant");
        assert_eq!(r.by_tag["NM"].query_present, 1);
        assert_eq!(r.by_tag["NM"].baseline_present, 1);
        assert_eq!(r.by_tag["NM"].value_diff, 0, "nothing diverged");
    }

    /// A primary present on one side only still contributes that side's tags:
    /// the comparison observed them, whatever the other side did.
    #[test]
    fn one_sided_records_contribute_one_sided_presence() {
        let mut r = ConcordanceReport::default();
        let rec = with_tags(&[("MQ", 60)]);
        r.record_presence(Some(&rec), None);
        assert_eq!(r.by_tag["MQ"].query_present, 1);
        assert_eq!(r.by_tag["MQ"].baseline_present, 0);
    }

    /// `ignored` is otherwise only set when a tag diverges, so a correctly
    /// ignored tag that happens to agree would read as un-ignored in the JSON.
    #[test]
    fn mark_ignored_flags_a_tag_that_never_diverged() {
        let mut r = ConcordanceReport::default();
        let rec = with_tags(&[("MQ", 60)]);
        r.record_presence(Some(&rec), Some(&rec));
        r.mark_ignored("MQ");
        assert!(r.by_tag["MQ"].ignored);
        assert_eq!(r.by_tag["MQ"].query_present, 1);
    }

    #[test]
    fn concordance_pct_counts_only_fully_matching_reads() {
        let mut r = ConcordanceReport::default();
        r.record(&scored(vec![]));
        r.record(&scored(vec![]));
        r.record(&scored(vec![tag_value("XS")]));
        r.record(&scored(vec![Discordance::CigarDiff]));
        r.finalize();
        assert!((r.concordance_pct - 50.0).abs() < f64::EPSILON);
        assert!((r.by_class["tag_diff"].pct - 25.0).abs() < f64::EPSILON);
    }
}
