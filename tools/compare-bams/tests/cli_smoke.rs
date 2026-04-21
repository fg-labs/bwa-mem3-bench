use std::path::PathBuf;
use std::process::Command;

use noodles_bam as bam;
use noodles_sam::alignment::io::Write as _;
use noodles_sam::alignment::record::Flags;
use noodles_sam::alignment::record_buf::RecordBuf;
use noodles_sam::header::record::value::{map, Map};
use noodles_sam::header::Header;
use std::num::NonZeroUsize;

fn write_bam(records: &[(&str, u16, Option<usize>)], path: &std::path::Path) {
    // No @HD SO tag — compare-bams no longer requires name-sorted input.
    let header_map = Map::<map::Header>::default();
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
    let bytes = writer.into_inner().into_inner();
    std::fs::write(path, bytes).expect("write file");
}

#[test]
fn cli_produces_json_report() {
    let tmp = tempfile::tempdir().unwrap();
    let q: PathBuf = tmp.path().join("q.bam");
    let b: PathBuf = tmp.path().join("b.bam");
    let out: PathBuf = tmp.path().join("report.json");
    write_bam(&[("r1", 0x63, Some(100))], &q);
    write_bam(&[("r1", 0x63, Some(100))], &b);

    let bin = env!("CARGO_BIN_EXE_compare-bams");
    let status = Command::new(bin)
        .args([
            "--query",
            q.to_str().unwrap(),
            "--baseline",
            b.to_str().unwrap(),
            "--out",
            out.to_str().unwrap(),
        ])
        .status()
        .unwrap();
    assert!(status.success(), "compare-bams CLI failed");

    let text = std::fs::read_to_string(&out).unwrap();
    let value: serde_json::Value =
        serde_json::from_str(&text).expect("output should be valid JSON");
    assert_eq!(value["total_reads"], 1);
    assert_eq!(value["concordant"], 1);
}

#[test]
fn cli_accepts_optional_flags() {
    let tmp = tempfile::tempdir().unwrap();
    let q: PathBuf = tmp.path().join("q.bam");
    let b: PathBuf = tmp.path().join("b.bam");
    let out: PathBuf = tmp.path().join("report.json");
    write_bam(&[("r1", 0x63, Some(100))], &q);
    write_bam(&[("r1", 0x63, Some(100))], &b);

    let bin = env!("CARGO_BIN_EXE_compare-bams");
    let status = Command::new(bin)
        .args([
            "--query",
            q.to_str().unwrap(),
            "--baseline",
            b.to_str().unwrap(),
            "--out",
            out.to_str().unwrap(),
            "--ignore-tag",
            "YD",
            "--ignore-tag",
            "XM",
            "--mapq-tolerance",
            "5",
        ])
        .status()
        .unwrap();
    assert!(status.success(), "compare-bams CLI rejected optional flags");

    let text = std::fs::read_to_string(&out).unwrap();
    let value: serde_json::Value =
        serde_json::from_str(&text).expect("output should be valid JSON");
    assert_eq!(value["total_reads"], 1);
    assert_eq!(value["concordant"], 1);
}
