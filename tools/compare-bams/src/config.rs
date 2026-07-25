//! User-tunable options for BAM comparison.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

/// Options controlling how [`crate::classify::classify`] compares two records.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CompareOptions {
    /// Aux tags excluded from comparison entirely — neither their presence nor
    /// their value is compared. Every other tag present on either record is
    /// compared strictly, and a difference counts against concordance.
    ///
    /// The list is a property of the *comparison kind*, not of the sample:
    ///
    /// * against upstream `bwa-mem2` — `MQ`, `HN`, which `bwa-mem3` emits and
    ///   upstream never does. Comparing either scores 0% concordance.
    /// * against `bwameth.py` — additionally `NM`, `MD`, `XA`, `SA` (each is,
    ///   or embeds, an edit distance computed against a C→T/G→A converted
    ///   reference, plus doubled-reference contig names such as `fchr1`), and
    ///   the disjoint bisulfite tag sets `XM`/`XG`/`XR` and `YD`/`YC`/`RG`.
    /// * `bwa-mem3` against itself at the same search settings (another release
    ///   or arch) — empty. Both sides are the same binary run the same way, so
    ///   every tag is comparable and any difference is a real finding.
    /// * `bwa-mem3` `--fast` against its own default — the candidate-set tags
    ///   (`XS`, `HN`, `XA`, `SA`, `MQ`) are excluded, because the preset prunes
    ///   that set by design and they diverge mechanically without carrying
    ///   placement information. The chosen-alignment tags (`AS`, `MD`, `NM`,
    ///   `MC`) stay strict.
    ///
    /// Divergence is still tallied per tag in the report regardless of this
    /// list (see [`crate::report::TagCounter`]), so skipping a tag hides it
    /// from the score, never from the diagnosis.
    pub ignore_tags: BTreeSet<String>,

    /// Permitted absolute MAPQ difference before flagging as discordant.
    pub mapq_tolerance: u8,
}
