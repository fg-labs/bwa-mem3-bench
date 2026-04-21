//! Classify a pair of alignment records as concordant or discordant by class.

use noodles_sam::alignment::record_buf::RecordBuf;
use serde::{Deserialize, Serialize};

use crate::config::CompareOptions;

/// Result of comparing two alignment records.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Discordance {
    Concordant,
    MappedOnlyQuery,
    MappedOnlyBaseline,
    /// Different chromosome, different position, or both.
    PosDiff {
        query_ref: Option<usize>,
        query_pos: Option<i64>,
        baseline_ref: Option<usize>,
        baseline_pos: Option<i64>,
    },
    CigarDiff,
    MapqDiff {
        query: u8,
        baseline: u8,
    },
    FlagDiff {
        query: u16,
        baseline: u16,
    },
    SecondarySetDiff,
}

/// Compare two records representing the same read in query and baseline BAMs.
///
/// Returns [`Discordance::Concordant`] when the two agree on primary-alignment
/// position, CIGAR, MAPQ (within tolerance), and flag bits that affect placement.
///
/// # Panics
///
/// Does not panic: all optional fields on the records are handled via
/// [`Option::map`] and [`Option::unwrap_or`].
#[must_use]
pub fn classify(query: &RecordBuf, baseline: &RecordBuf, opts: &CompareOptions) -> Discordance {
    let q_mapped = !query.flags().is_unmapped();
    let b_mapped = !baseline.flags().is_unmapped();
    match (q_mapped, b_mapped) {
        (false, false) => return Discordance::Concordant,
        (true, false) => return Discordance::MappedOnlyQuery,
        (false, true) => return Discordance::MappedOnlyBaseline,
        (true, true) => {}
    }

    let q_ref = query.reference_sequence_id();
    let b_ref = baseline.reference_sequence_id();
    let q_pos = query
        .alignment_start()
        .map(|p| i64::try_from(usize::from(p)).unwrap_or(i64::MAX));
    let b_pos = baseline
        .alignment_start()
        .map(|p| i64::try_from(usize::from(p)).unwrap_or(i64::MAX));
    if q_ref != b_ref || q_pos != b_pos {
        return Discordance::PosDiff {
            query_ref: q_ref,
            query_pos: q_pos,
            baseline_ref: b_ref,
            baseline_pos: b_pos,
        };
    }

    if query.cigar().as_ref() != baseline.cigar().as_ref() {
        return Discordance::CigarDiff;
    }

    let q_mapq = query.mapping_quality().map_or(0, u8::from);
    let b_mapq = baseline.mapping_quality().map_or(0, u8::from);
    if q_mapq.abs_diff(b_mapq) > opts.mapq_tolerance {
        return Discordance::MapqDiff {
            query: q_mapq,
            baseline: b_mapq,
        };
    }

    let placement_mask = 0x10 | 0x20 | 0x40 | 0x80; // strand + mate strand + r1 + r2
    let q_flags = query.flags().bits() & placement_mask;
    let b_flags = baseline.flags().bits() & placement_mask;
    if q_flags != b_flags {
        return Discordance::FlagDiff {
            query: q_flags,
            baseline: b_flags,
        };
    }

    Discordance::Concordant
}
