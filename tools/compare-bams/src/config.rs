//! User-tunable options for BAM comparison.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

/// Options controlling how [`crate::classify::classify`] compares two records.
#[derive(Debug, Clone, Serialize, Deserialize)]
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

    /// Aux tags that MAY appear. Any tag observed on either side that is in
    /// neither this set nor `ignore_tags` is a
    /// [`crate::guard::TagGuardViolation::UnexpectedTag`].
    ///
    /// The semantics are deliberately *may* appear, not *must*: a tag listed
    /// here that never shows up is a harmless no-op. That keeps one list usable
    /// across samples whose tag sets legitimately differ — single-end reads
    /// carry no mate tags, bisulfite alignment carries six extra ones — without
    /// needing a per-sample subtraction operation.
    ///
    /// An empty set means the allowlist is unconfigured, and the unexpected-tag
    /// check is skipped: there is no way to distinguish "no allowlist" from
    /// "the allowlist is empty", and failing every tag would be useless. The
    /// workflow closes that hole in config validation instead, by requiring
    /// every comparison kind to declare a non-empty set.
    pub expect_tags: BTreeSet<String>,

    /// `ignore_tags` entries exempt from the dead-entry check, because they are
    /// known to be absent from this particular comparison.
    ///
    /// This exempts an entry from the *audit* only; it does not change what is
    /// ignored. That distinction matters. `MQ` and `HN` are absent from both
    /// sides of every methylation comparison (fg-labs/bwa-mem3#296), but they
    /// remain correctly ignored there — bwameth would never emit them even once
    /// `bwa-mem3` does. Dropping them from `ignore_tags` instead would make them
    /// strict, and the day #296 is fixed the meth comparison would score ~0%.
    pub absent_ok_tags: BTreeSet<String>,

    /// Permitted absolute MAPQ difference before flagging as discordant.
    pub mapq_tolerance: u8,

    /// Whether to run the tag-set guard at all (`--no-tag-guard` clears it).
    ///
    /// Defaults to **true**, which is why this type implements [`Default`] by
    /// hand rather than deriving it. bench #34 was config that had to be opted
    /// into and never was; a guard against that failure must not itself need
    /// opting into.
    pub tag_guard: bool,
}

impl Default for CompareOptions {
    fn default() -> Self {
        Self {
            ignore_tags: BTreeSet::new(),
            expect_tags: BTreeSet::new(),
            absent_ok_tags: BTreeSet::new(),
            mapq_tolerance: 0,
            tag_guard: true,
        }
    }
}
