use compare_bams::pair_reader::{pair_iter, Pair};
use noodles_bam as bam;
use noodles_sam::alignment::io::Write as _;
use noodles_sam::alignment::record::Flags;
use noodles_sam::alignment::record_buf::RecordBuf;
use noodles_sam::header::record::value::{map, Map};
use noodles_sam::header::Header;
use std::io::Cursor;

/// Build a BAM in memory with no @HD SO tag — mirrors `bwa-mem2` output.
fn write_bam(records: &[(&str, u16)]) -> Vec<u8> {
    let hd_map = Map::<map::Header>::default();
    let header = Header::builder().set_header(hd_map).build();

    let mut writer = bam::io::Writer::new(Vec::new());
    writer.write_header(&header).unwrap();
    for (name, flags) in records {
        let mut r = RecordBuf::default();
        *r.name_mut() = Some((*name).into());
        *r.flags_mut() = Flags::from_bits_truncate(*flags);
        writer.write_alignment_record(&header, &r).unwrap();
    }
    writer.try_finish().unwrap();
    writer.into_inner().into_inner()
}

#[test]
fn lockstep_pairs_emit_in_order() {
    let q = write_bam(&[("r1", 0x63), ("r2", 0x63)]);
    let b = write_bam(&[("r1", 0x63), ("r2", 0x63)]);

    let pairs: Vec<Pair> = pair_iter(Cursor::new(q), Cursor::new(b))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();

    assert_eq!(pairs.len(), 2);
    assert!(matches!(pairs[0], Pair::Both { .. }));
    assert!(matches!(pairs[1], Pair::Both { .. }));
}

#[test]
fn unsorted_bams_are_accepted() {
    // BAM with no @HD SO tag (default `bwa-mem2` output) — must not be rejected.
    let buf = write_bam(&[("r1", 0x63)]);
    let pairs: Vec<Pair> = pair_iter(Cursor::new(buf.clone()), Cursor::new(buf))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    assert_eq!(pairs.len(), 1);
    assert!(matches!(pairs[0], Pair::Both { .. }));
}

#[test]
fn diverging_qname_order_errors() {
    // Both streams have {r1, r2} but in opposite order — lockstep walk must fail.
    let q = write_bam(&[("r1", 0x63), ("r2", 0x63)]);
    let b = write_bam(&[("r2", 0x63), ("r1", 0x63)]);

    let results: Vec<_> = pair_iter(Cursor::new(q), Cursor::new(b)).unwrap().collect();
    // First step compares ("r1","r2") → Err.
    assert!(
        results.first().is_some_and(Result::is_err),
        "expected first pair to be Err on qname divergence; got {results:?}"
    );
}

#[test]
fn query_longer_than_baseline_emits_query_only_tail() {
    let q = write_bam(&[("r1", 0x63), ("r2", 0x63)]);
    let b = write_bam(&[("r1", 0x63)]);

    let pairs: Vec<Pair> = pair_iter(Cursor::new(q), Cursor::new(b))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();

    assert_eq!(pairs.len(), 2);
    assert!(matches!(pairs[0], Pair::Both { .. }));
    assert!(matches!(pairs[1], Pair::QueryOnly(_)));
}

#[test]
fn baseline_longer_than_query_emits_baseline_only_tail() {
    let q = write_bam(&[("r1", 0x63)]);
    let b = write_bam(&[("r1", 0x63), ("r2", 0x63)]);

    let pairs: Vec<Pair> = pair_iter(Cursor::new(q), Cursor::new(b))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();

    assert_eq!(pairs.len(), 2);
    assert!(matches!(pairs[0], Pair::Both { .. }));
    assert!(matches!(pairs[1], Pair::BaselineOnly(_)));
}
