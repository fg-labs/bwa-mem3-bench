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
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ConcordanceReport {
    pub total_reads: u64,
    pub concordant: u64,
    pub concordance_pct: f64,
    pub by_class: BTreeMap<String, ClassCounter>,
}

impl ConcordanceReport {
    /// Record a single classified read into the report counters.
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

    /// Compute derived percentages. Call once after all records have been fed via [`record`].
    pub fn finalize(&mut self) {
        if self.total_reads == 0 {
            return;
        }
        #[allow(clippy::cast_precision_loss)]
        let total = self.total_reads as f64;
        #[allow(clippy::cast_precision_loss)]
        let concordant = self.concordant as f64;
        self.concordance_pct = concordant / total * 100.0;
        for v in self.by_class.values_mut() {
            #[allow(clippy::cast_precision_loss)]
            let count = v.count as f64;
            v.pct = count / total * 100.0;
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
