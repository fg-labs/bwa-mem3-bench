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

/// The shared single-contig header both writers emit.
fn test_header() -> Header {
    // No @HD SO tag — compare-bams no longer requires name-sorted input.
    let ref_seq = Map::<map::ReferenceSequence>::new(NonZeroUsize::try_from(1_000_000).unwrap());
    Header::builder()
        .set_header(Map::<map::Header>::default())
        .add_reference_sequence("chr1", ref_seq)
        .build()
}

fn write_records(header: &Header, records: &[RecordBuf], path: &std::path::Path) {
    let mut writer = bam::io::Writer::new(Vec::new());
    writer.write_header(header).expect("write header");
    for r in records {
        writer
            .write_alignment_record(header, r)
            .expect("write record");
    }
    writer.try_finish().expect("finish");
    std::fs::write(path, writer.into_inner().into_inner()).expect("write file");
}

/// A one-record BAM whose single mapped primary carries `tags`, each `i:1`.
fn write_bam_with_tags(tags: &[&str], path: &std::path::Path) {
    let data: Data = tags
        .iter()
        .map(|t| {
            let b = t.as_bytes();
            (Tag::new(b[0], b[1]), Value::Int32(1))
        })
        .collect();

    let mut r = RecordBuf::default();
    *r.name_mut() = Some("r1".into());
    *r.flags_mut() = Flags::from_bits_truncate(0x63);
    *r.reference_sequence_id_mut() = Some(0);
    *r.alignment_start_mut() = 100usize.try_into().ok();
    *r.data_mut() = data;

    write_records(&test_header(), &[r], path);
}

#[test]
fn cli_produces_json_report() {
    let (code, value) = run_guarded(&[], &[], &["--no-tag-guard"]);
    assert_eq!(code, 0, "compare-bams CLI failed");
    assert_eq!(value["total_reads"], 1);
    assert_eq!(value["concordant"], 1);
}

#[test]
fn cli_accepts_optional_flags() {
    let (code, value) = run_guarded(
        &["NM"],
        &["NM"],
        &[
            "--ignore-tag",
            "YD",
            "--ignore-tag",
            "XM",
            "--expect-tag",
            "NM",
            // Neither record carries YD or XM, so both ignore entries are dead;
            // excuse them rather than turning the guard off, which keeps this
            // test on the guard's normal path.
            "--absent-ok-tag",
            "YD",
            "--absent-ok-tag",
            "XM",
            "--mapq-tolerance",
            "5",
        ],
    );
    assert_eq!(code, 0, "compare-bams CLI rejected optional flags");
    assert_eq!(value["total_reads"], 1);
    assert_eq!(value["concordant"], 1);
    assert!(
        value.get("tag_guard_violations").is_none(),
        "a clean run must not emit the violations key"
    );
}

/// Run the CLI over two one-record BAMs carrying the given tags.
///
/// Returns the exit code and the parsed report. The report is read
/// unconditionally, because every guard failure must still leave it on disk.
fn run_guarded(
    query_tags: &[&str],
    baseline_tags: &[&str],
    extra: &[&str],
) -> (i32, serde_json::Value) {
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
    let value = serde_json::from_str(&text).expect("output should be valid JSON");
    (status.code().unwrap(), value)
}

/// The headline behaviour: an unanticipated tag fails the run by name, and the
/// report is still on disk so the failure can be diagnosed from `by_tag`.
#[test]
fn an_unexpected_tag_exits_3_and_still_writes_the_report() {
    let (code, value) = run_guarded(&["NM", "ZZ"], &["NM", "ZZ"], &["--expect-tag", "NM"]);
    assert_eq!(code, 3, "tag-guard failures use a distinct exit code");

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
    let (code, value) = run_guarded(
        &["NM"],
        &["NM"],
        &["--expect-tag", "NM", "--ignore-tag", "MQ"],
    );
    assert_eq!(code, 3);
    let violations = value["tag_guard_violations"].as_array().unwrap();
    assert_eq!(violations[0]["kind"], "dead_ignore_entry");
    assert_eq!(violations[0]["tag"], "MQ");
    // The dead entry is also stated in by_tag -- flagged ignored, with all-zero
    // counts -- so the report describes the whole policy, not just what fired.
    assert_eq!(value["by_tag"]["MQ"]["ignored"], true);
    assert_eq!(value["by_tag"]["MQ"]["query_present"], 0);
    assert_eq!(value["by_tag"]["MQ"]["baseline_present"], 0);
}

/// An ignored tag that IS present and never diverges must still read as ignored
/// in `by_tag`; `ignored` is otherwise only set when a tag diverges, so the
/// block would understate the policy.
#[test]
fn a_live_ignore_entry_that_never_diverges_is_still_flagged_ignored() {
    let (code, value) = run_guarded(
        &["NM", "MQ"],
        &["NM", "MQ"],
        &["--expect-tag", "NM", "--ignore-tag", "MQ"],
    );
    assert_eq!(code, 0, "MQ is present, so the entry is not dead");
    assert_eq!(value["by_tag"]["MQ"]["ignored"], true);
    assert_eq!(value["by_tag"]["MQ"]["query_present"], 1);
    assert_eq!(value["by_tag"]["MQ"]["value_diff"], 0);
}

/// Excusing a tag nobody ignores is inert config, and the guard holds its own
/// inputs to the rule it enforces.
#[test]
fn a_redundant_absent_ok_entry_exits_3() {
    let (code, value) = run_guarded(
        &["NM"],
        &["NM"],
        &["--expect-tag", "NM", "--absent-ok-tag", "ZZ"],
    );
    assert_eq!(code, 3);
    let violations = value["tag_guard_violations"].as_array().unwrap();
    assert_eq!(violations[0]["kind"], "redundant_absent_ok");
    assert_eq!(violations[0]["tag"], "ZZ");
}

#[test]
fn no_tag_guard_suppresses_the_failure_but_not_the_report() {
    let (code, value) = run_guarded(
        &["NM", "ZZ"],
        &["NM", "ZZ"],
        &["--expect-tag", "NM", "--no-tag-guard"],
    );
    assert_eq!(code, 0);
    assert!(value.get("tag_guard_violations").is_none());
    // The tag is still visible in by_tag; the guard changes the verdict, not
    // what is reported.
    assert_eq!(value["by_tag"]["ZZ"]["query_present"], 1);
}

/// An unconfigured allowlist cannot be enforced — the tool would have to skip
/// the unexpected-tag check silently. Rather than let that happen, the CLI makes
/// the choice explicit: either declare the allowlist or ask for the guard off.
/// clap rejects the omission at startup (exit 2), before any BAM is read, so the
/// guard can only be inert because a caller said so.
#[test]
fn omitting_both_expect_tag_and_no_tag_guard_is_a_usage_error() {
    let tmp = tempfile::tempdir().unwrap();
    let q = tmp.path().join("q.bam");
    let b = tmp.path().join("b.bam");
    write_bam_with_tags(&["NM"], &q);
    write_bam_with_tags(&["NM"], &b);

    let out = Command::new(env!("CARGO_BIN_EXE_compare-bams"))
        .args([
            "--query",
            q.to_str().unwrap(),
            "--baseline",
            b.to_str().unwrap(),
            "--out",
            tmp.path().join("report.json").to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2), "clap usage error");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(stderr.contains("--expect-tag"), "{stderr}");
    assert!(
        !tmp.path().join("report.json").exists(),
        "must fail before doing any work"
    );
}

/// The guard's own CLI is held to the rule the guard enforces: a tag name that
/// can never match a record is inert config, so it is rejected at the boundary
/// rather than silently allowlisting/ignoring nothing.
#[test]
fn a_malformed_tag_name_is_a_usage_error() {
    let tmp = tempfile::tempdir().unwrap();
    let q = tmp.path().join("q.bam");
    let b = tmp.path().join("b.bam");
    write_bam_with_tags(&["NM"], &q);
    write_bam_with_tags(&["NM"], &b);

    for (flag, bad) in [
        ("--expect-tag", "XYZ"),
        ("--ignore-tag", "1N"),
        ("--absent-ok-tag", "@X"),
        ("--expect-tag", "N"),
    ] {
        let out = Command::new(env!("CARGO_BIN_EXE_compare-bams"))
            .args([
                "--query",
                q.to_str().unwrap(),
                "--baseline",
                b.to_str().unwrap(),
                "--out",
                tmp.path().join("report.json").to_str().unwrap(),
                "--expect-tag",
                "NM",
                flag,
                bad,
            ])
            .output()
            .unwrap();
        assert_eq!(out.status.code(), Some(2), "{flag} {bad}: clap usage error");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert!(
            stderr.contains("SAM aux tag name"),
            "{flag} {bad}: {stderr}"
        );
        assert!(
            !tmp.path().join("report.json").exists(),
            "{flag} {bad}: must fail before doing any work"
        );
    }
}
