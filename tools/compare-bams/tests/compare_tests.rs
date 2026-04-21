use compare_bams::{compare, CompareOptions};
use noodles_bam as bam;
use noodles_sam::alignment::io::Write as _;
use noodles_sam::alignment::record::Flags;
use noodles_sam::alignment::record_buf::RecordBuf;
use noodles_sam::header::record::value::{map, Map};
use noodles_sam::header::Header;
use std::io::Cursor;
use std::num::NonZeroUsize;

fn write_bam(records: &[(&str, u16, Option<usize>)]) -> Vec<u8> {
    // No @HD SO tag — mirrors the unsorted BAMs bwa-mem2 produces.
    let header_map = Map::<map::Header>::default();

    // Add a single reference sequence so reference_sequence_id = 0 is valid.
    let ref_seq = Map::<map::ReferenceSequence>::new(NonZeroUsize::try_from(1_000_000).unwrap());
    let header = Header::builder()
        .set_header(header_map)
        .add_reference_sequence("chr1", ref_seq)
        .build();

    let mut writer = bam::io::Writer::new(Vec::new());
    writer.write_header(&header).expect("write header");
    for (name, flags, pos) in records {
        let mut r = RecordBuf::default();
        *r.name_mut() = Some((*name).into());
        *r.flags_mut() = Flags::from_bits_truncate(*flags);
        *r.reference_sequence_id_mut() = pos.map(|_| 0);
        *r.alignment_start_mut() = pos.and_then(|p| p.try_into().ok());
        writer
            .write_alignment_record(&header, &r)
            .expect("write record");
    }
    writer.try_finish().expect("finish");
    writer.into_inner().into_inner()
}

#[test]
fn identical_streams_report_100_percent() {
    let q = write_bam(&[("r1", 0x63, Some(100)), ("r2", 0x63, Some(200))]);
    let b = q.clone();
    let rep = compare(Cursor::new(q), Cursor::new(b), &CompareOptions::default()).unwrap();
    assert_eq!(rep.total_reads, 2);
    assert_eq!(rep.concordant, 2);
    assert!((rep.concordance_pct - 100.0).abs() < 1e-9);
}

#[test]
fn pos_diff_counts_one_discordance() {
    let q = write_bam(&[("r1", 0x63, Some(100))]);
    let b = write_bam(&[("r1", 0x63, Some(150))]);
    let rep = compare(Cursor::new(q), Cursor::new(b), &CompareOptions::default()).unwrap();
    assert_eq!(rep.total_reads, 1);
    assert_eq!(rep.concordant, 0);
    assert_eq!(rep.by_class.get("pos_diff").unwrap().count, 1);
}
