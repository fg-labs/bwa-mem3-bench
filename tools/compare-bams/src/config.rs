//! User-tunable options for BAM comparison.

use serde::{Deserialize, Serialize};

/// Options controlling how [`crate::classify::classify`] compares two records.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CompareOptions {
    /// Tags to ignore when comparing (e.g. `["YD", "XM", "XG"]` for meth samples).
    pub ignore_tags: Vec<String>,

    /// Permitted absolute MAPQ difference before flagging as discordant.
    pub mapq_tolerance: u8,
}
