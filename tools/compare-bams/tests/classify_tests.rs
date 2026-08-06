use compare_bams::{classify, CompareOptions, Discordance};

// `classify` reports EVERY differing field, so these assert on the whole
// difference list: a single-element slice proves nothing else diverged.
use noodles_sam::alignment::record::Flags;
use noodles_sam::alignment::record_buf::RecordBuf;

fn record(
    name: &str,
    flags: u16,
    ref_id: Option<usize>,
    pos: Option<usize>,
    mapq: u8,
) -> RecordBuf {
    let mut r = RecordBuf::default();
    *r.name_mut() = Some(name.into());
    *r.flags_mut() = Flags::from_bits_truncate(flags);
    *r.reference_sequence_id_mut() = ref_id;
    *r.alignment_start_mut() = pos.and_then(|p| p.try_into().ok());
    *r.mapping_quality_mut() = mapq.try_into().ok();
    r
}

#[test]
fn identical_records_are_concordant() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(0), Some(100), 60);
    let opts = CompareOptions::default();
    assert!(classify(&a, &b, &opts).is_concordant());
}

#[test]
fn different_positions_are_pos_diff() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(0), Some(150), 60);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts).diffs.as_slice(),
        [Discordance::PosDiff { .. }]
    ));
}

#[test]
fn only_query_mapped_is_mapped_only_query() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x4, None, None, 0);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts).diffs.as_slice(),
        [Discordance::MappedOnlyQuery]
    ));
}

#[test]
fn mapq_within_tolerance_is_concordant() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(0), Some(100), 58);
    let opts = CompareOptions {
        mapq_tolerance: 2,
        ..Default::default()
    };
    assert!(classify(&a, &b, &opts).is_concordant());
}

#[test]
fn mapq_outside_tolerance_is_mapq_diff() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(0), Some(100), 40);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts).diffs.as_slice(),
        [Discordance::MapqDiff { .. }]
    ));
}

#[test]
fn only_baseline_mapped_is_mapped_only_baseline() {
    let a = record("r1", 0x4, None, None, 0);
    let b = record("r1", 0x63, Some(0), Some(100), 60);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts).diffs.as_slice(),
        [Discordance::MappedOnlyBaseline]
    ));
}

#[test]
fn different_chromosomes_is_pos_diff() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(1), Some(100), 60);
    let opts = CompareOptions::default();
    match classify(&a, &b, &opts).diffs.as_slice() {
        [Discordance::PosDiff {
            query_ref,
            baseline_ref,
            ..
        }] => {
            assert_eq!(*query_ref, Some(0));
            assert_eq!(*baseline_ref, Some(1));
        }
        other => panic!("expected a single PosDiff, got {other:?}"),
    }
}

#[test]
fn different_flag_bits_is_flag_diff() {
    // 0x63 = 99  = PAIRED|PROPER|MATE_REVERSE|R1
    // 0x53 = 83  = PAIRED|PROPER|REVERSE|R1
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x53, Some(0), Some(100), 60);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts).diffs.as_slice(),
        [Discordance::FlagDiff { .. }]
    ));
}

/// The proper-pair bit is aligner judgement, not a derived convenience, so a
/// disagreement on it is a finding. fg-labs/bwa-mem3#362 is exactly this bit
/// under ALT-aware alignment, and the old `0x10|0x20|0x40|0x80` placement mask
/// could not see it: 147 differing records in a measured 400,000-record ALT
/// cell scored as fully concordant.
#[test]
fn proper_pair_bit_difference_is_flag_diff() {
    // 0x63 = PAIRED|PROPER|MATE_REVERSE|R1, 0x61 = the same without PROPER.
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x61, Some(0), Some(100), 60);
    let opts = CompareOptions::default();
    match classify(&a, &b, &opts).diffs.as_slice() {
        [Discordance::FlagDiff { query, baseline }] => {
            assert_eq!(
                query ^ baseline,
                0x2,
                "only the proper-pair bit should differ"
            );
        }
        other => panic!("expected a single FlagDiff on 0x2, got {other:?}"),
    }
}

/// Mate-unmapped is the other bit carrying aligner judgement that the old mask
/// dropped — it moves when mate rescue behaves differently.
#[test]
fn mate_unmapped_bit_difference_is_flag_diff() {
    // 0x69 = PAIRED|MATE_UNMAPPED|MATE_REVERSE|R1, 0x61 = same without 0x8.
    let a = record("r1", 0x69, Some(0), Some(100), 60);
    let b = record("r1", 0x61, Some(0), Some(100), 60);
    let opts = CompareOptions::default();
    match classify(&a, &b, &opts).diffs.as_slice() {
        [Discordance::FlagDiff { query, baseline }] => {
            assert_eq!(
                query ^ baseline,
                0x8,
                "only the mate-unmapped bit should differ"
            );
        }
        other => panic!("expected a single FlagDiff on 0x8, got {other:?}"),
    }
}

/// Both-unmapped pairs used to return before the flag comparison was reached,
/// so every FLAG bit was exempt on exactly the population where mate-rescue
/// disagreement lives. Tags were still compared there; FLAG must be too.
#[test]
fn both_unmapped_records_still_compare_flags() {
    // 0x4D = PAIRED|UNMAPPED|MATE_UNMAPPED|R1, 0x45 = the same without 0x8.
    let a = record("r1", 0x4D, None, None, 0);
    let b = record("r1", 0x45, None, None, 0);
    let opts = CompareOptions::default();
    match classify(&a, &b, &opts).diffs.as_slice() {
        [Discordance::FlagDiff { query, baseline }] => {
            assert_eq!(query ^ baseline, 0x8);
        }
        other => panic!("expected a single FlagDiff on 0x8, got {other:?}"),
    }
}
