use compare_bams::{classify, CompareOptions, Discordance};
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
    assert!(matches!(classify(&a, &b, &opts), Discordance::Concordant));
}

#[test]
fn different_positions_are_pos_diff() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(0), Some(150), 60);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts),
        Discordance::PosDiff { .. }
    ));
}

#[test]
fn only_query_mapped_is_mapped_only_query() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x4, None, None, 0);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts),
        Discordance::MappedOnlyQuery
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
    assert!(matches!(classify(&a, &b, &opts), Discordance::Concordant));
}

#[test]
fn mapq_outside_tolerance_is_mapq_diff() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(0), Some(100), 40);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts),
        Discordance::MapqDiff { .. }
    ));
}

#[test]
fn only_baseline_mapped_is_mapped_only_baseline() {
    let a = record("r1", 0x4, None, None, 0);
    let b = record("r1", 0x63, Some(0), Some(100), 60);
    let opts = CompareOptions::default();
    assert!(matches!(
        classify(&a, &b, &opts),
        Discordance::MappedOnlyBaseline
    ));
}

#[test]
fn different_chromosomes_is_pos_diff() {
    let a = record("r1", 0x63, Some(0), Some(100), 60);
    let b = record("r1", 0x63, Some(1), Some(100), 60);
    let opts = CompareOptions::default();
    match classify(&a, &b, &opts) {
        Discordance::PosDiff {
            query_ref,
            baseline_ref,
            ..
        } => {
            assert_eq!(query_ref, Some(0));
            assert_eq!(baseline_ref, Some(1));
        }
        other => panic!("expected PosDiff, got {other:?}"),
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
        classify(&a, &b, &opts),
        Discordance::FlagDiff { .. }
    ));
}
