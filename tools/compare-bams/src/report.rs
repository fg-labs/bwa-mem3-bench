//! Aggregated comparison report, serializable to JSON.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::classify::Discordance;

/// Per-class count and percentage bucket in a [`ConcordanceReport`].
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ClassCounter {
    pub count: u64,
    pub pct: f64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub samples: Vec<String>,
}

/// Aggregated result of comparing two BAMs, suitable for JSON serialization.
///
/// `concordance_pct` and `by_class` cover **primary** alignments only (one unit
/// per read end), so the headline number stays comparable across aligner
/// versions even when supplementary emission differs. The `supp_*` fields report
/// supplementary disagreement as a separate, non-fatal axis. All fields are
/// additive over the historical schema, so existing JSON consumers keep working.
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ConcordanceReport {
    /// Total primary alignment records compared (denominator of `concordance_pct`).
    pub total_reads: u64,
    pub concordant: u64,
    pub concordance_pct: f64,
    pub by_class: BTreeMap<String, ClassCounter>,

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
    /// Record a single classified primary read into the report counters.
    pub fn record(&mut self, d: &Discordance) {
        self.total_reads += 1;
        match d {
            Discordance::Concordant => self.concordant += 1,
            other => {
                let key = discordance_key(other);
                let entry = self.by_class.entry(key.to_string()).or_default();
                entry.count += 1;
            }
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

    /// Compute derived percentages. Call once after all records have been fed via [`record`].
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

fn discordance_key(d: &Discordance) -> &'static str {
    match d {
        Discordance::Concordant => "concordant",
        Discordance::MappedOnlyQuery => "mapped_only_query",
        Discordance::MappedOnlyBaseline => "mapped_only_baseline",
        Discordance::PosDiff { .. } => "pos_diff",
        Discordance::CigarDiff => "cigar_diff",
        Discordance::MapqDiff { .. } => "mapq_diff",
        Discordance::FlagDiff { .. } => "flag_diff",
        Discordance::SecondarySetDiff => "secondary_set_diff",
    }
}
