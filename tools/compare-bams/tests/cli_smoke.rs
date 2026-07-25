use std::path::PathBuf;
use std::process::Command;

use noodles_bam as bam;
use noodles_sam::alignment::io::Write as _;
use noodles_sam::alignment::record::data::field::Tag;
use noodles_sam::alignment::record::Flags;
use noodles_sam::alignment::record_buf::data::field::Value;
use noodles_sam::alignment::record_buf::{Data, RecordBuf};
use noodles_sam::header::record::value::{map, Map};
use noodles_sam::header::Header;
use std::num::NonZeroUsize;

/// A one-record BAM whose single primary carries `tags` on both mates' behalf.
fn write_bam_with_tags(tags: &[&str], path: &std::path::Path) {
    let header_map = Map::<map::Header>::default();
    let ref_seq = Map::<map::ReferenceSequence>::new(NonZeroUsize::try_from(1_000_000).unwrap());
    let header = Header::builder()
        .set_header(header_map)
        .add_reference_sequence("chr1", ref_seq)
        .build();

    let data: Data = tags
        .iter()
        .map(|t| {
            let b = t.as_bytes();
            (Tag::new(b[0], b[1]), Value::Int32(1))
        })
        .collect::<Vec<_>>()
        .into_iter()
        .collect();

    let mut r = RecordBuf::default();
    *r.name_mut() = Some("r1".into());
    *r.flags_mut() = Flags::from_bits_truncate(0x63);
    *r.reference_sequence_id_mut() = Some(0);
    *r.alignment_start_mut() = 100usize.try_into().ok();
    *r.data_mut() = data;

    let mut writer = bam::io::Writer::new(Vec::new());
    writer.write_header(&header).expect("write header");
    writer
        .write_alignment_record(&header, &r)
        .expect("write record");
    writer.try_finish().expect("finish");
    let bytes = writer.into_inner().into_inner();
    std::fs::write(path, bytes).expect("write file");
}

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
            "--expect-tag",
            "NM",
            // These records carry no tags at all, so both ignore entries are
            // dead; excuse them rather than turning the guard off, which keeps
            // this test exercising the guard's normal path.
            "--absent-ok-tag",
            "YD",
            "--absent-ok-tag",
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
    assert!(
        value.get("tag_guard_violations").is_none(),
        "a clean run must not emit the violations key"
    );
}

/// Run the CLI and return `(exit code, parsed report)`.
fn run_guarded(query_tags: &[&str], baseline_tags: &[&str], extra: &[&str]) -> (i32, String) {
    let tmp = tempfile::tempdir().unwrap();
    let q: PathBuf = tmp.path().join("q.bam");
    let b: PathBuf = tmp.path().join("b.bam");
    let out: PathBuf = tmp.path().join("report.json");
    write_bam_with_tags(query_tags, &q);
    write_bam_with_tags(baseline_tags, &b);

    let bin = env!("CARGO_BIN_EXE_compare-bams");
    let mut args = vec![
        "--query",
        q.to_str().unwrap(),
        "--baseline",
        b.to_str().unwrap(),
        "--out",
        out.to_str().unwrap(),
    ];
    args.extend_from_slice(extra);
    let status = Command::new(bin).args(&args).status().unwrap();
    let text = std::fs::read_to_string(&out).expect("report must be written even on failure");
    (status.code().unwrap(), text)
}

/// The headline behaviour: an unanticipated tag fails the run by name, and the
/// report is still on disk so the failure can be diagnosed from `by_tag`.
#[test]
fn an_unexpected_tag_exits_3_and_still_writes_the_report() {
    let (code, text) = run_guarded(&["NM", "ZZ"], &["NM", "ZZ"], &["--expect-tag", "NM"]);
    assert_eq!(code, 3, "tag-guard failures use a distinct exit code");

    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let violations = value["tag_guard_violations"].as_array().unwrap();
    assert_eq!(violations.len(), 1, "{violations:?}");
    assert_eq!(violations[0]["kind"], "unexpected_tag");
    assert_eq!(violations[0]["tag"], "ZZ");
    // Presence is reported for every observed tag, not only diverging ones.
    assert_eq!(value["by_tag"]["NM"]["query_present"], 1);
    assert_eq!(value["by_tag"]["NM"]["value_diff"], 0);
}

#[test]
fn a_dead_ignore_entry_exits_3() {
    let (code, text) = run_guarded(
        &["NM"],
        &["NM"],
        &["--expect-tag", "NM", "--ignore-tag", "MQ"],
    );
    assert_eq!(code, 3);
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    let violations = value["tag_guard_violations"].as_array().unwrap();
    assert_eq!(violations[0]["kind"], "dead_ignore_entry");
    assert_eq!(violations[0]["tag"], "MQ");
}

#[test]
fn no_tag_guard_suppresses_the_failure_but_not_the_report() {
    let (code, text) = run_guarded(
        &["NM", "ZZ"],
        &["NM", "ZZ"],
        &["--expect-tag", "NM", "--no-tag-guard"],
    );
    assert_eq!(code, 0);
    let value: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert!(value.get("tag_guard_violations").is_none());
    // The tag is still visible in by_tag; the guard changes the verdict, not
    // what is reported.
    assert_eq!(value["by_tag"]["ZZ"]["query_present"], 1);
}

/// An unconfigured allowlist cannot be enforced, so it must not fail every run
/// that happens not to pass `--expect-tag` — the workflow guarantees the flag.
#[test]
fn omitting_expect_tag_disables_only_the_unexpected_tag_check() {
    let (code, _) = run_guarded(&["NM", "ZZ"], &["NM", "ZZ"], &[]);
    assert_eq!(code, 0);
}
