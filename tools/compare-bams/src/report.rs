//! Aggregated comparison report, serializable to JSON.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::classify::{Classification, Discordance};

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
    /// True when this tag is on the `ignore_tags` list, so its divergence is
    /// reported here but excluded from `concordance_pct`. Present so a reader
    /// can tell "diverges and we accepted it" from "diverges and it counted".
    #[serde(default)]
    pub ignored: bool,
}

/// Aggregated result of comparing two BAMs, suitable for JSON serialization.
///
/// `concordance_pct` and `by_class` cover **primary** alignments only (one unit
/// per read end), so the headline number stays comparable across aligner
/// versions even when supplementary emission differs. The `supp_*` fields report
/// supplementary disagreement as a separate, non-fatal axis. All fields are
/// additive over the historical schema, so existing JSON consumers keep working.
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

    /// Per-tag divergence, keyed by tag name. Populated for EVERY tag that
    /// diverged, including those on `ignore_tags` — those carry `ignored: true`
    /// and are excluded from `concordance_pct` but never hidden, so a
    /// misclassified tag is diagnosable from this block alone.
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

    /// Record one template's supplementary tallies.
    pub fn record_supplementary(&mut self, query_total: u64, baseline_total: u64, unmatched: u64) {
        self.total_templates += 1;
        self.supp_query_total += query_total;
        self.supp_baseline_total += baseline_total;
        if query_total != baseline_total {
            self.supp_count_mismatch_templates += 1;
        }
        self.supp_unmatched += unmatched;
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
            self.supp_count_mismatch_pct =
                self.supp_count_mismatch_templates as f64 / self.total_templates as f64 * 100.0;
        }

        let supp_total = self.supp_query_total + self.supp_baseline_total;
        #[allow(clippy::cast_precision_loss)]
        if supp_total > 0 {
            self.supp_unmatched_pct = self.supp_unmatched as f64 / supp_total as f64 * 100.0;
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
fn discordance_key(d: &Discordance) -> &'static str {
    match d {
        Discordance::MappedOnlyQuery => "mapped_only_query",
        Discordance::MappedOnlyBaseline => "mapped_only_baseline",
        Discordance::PosDiff { .. } => "pos_diff",
        Discordance::CigarDiff => "cigar_diff",
        Discordance::MapqDiff { .. } => "mapq_diff",
        Discordance::FlagDiff { .. } => "flag_diff",
        Discordance::SecondarySetDiff => "secondary_set_diff",
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
