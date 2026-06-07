//! End-to-end tests for the template-grouped reader over real in-memory BAMs.
//!
//! These exercise the full path (`record_stream` → `Grouper` → `Templates`) that
//! the in-module unit tests bypass by feeding record vectors directly.

use compare_bams::template_iter;
use noodles_bam as bam;
use noodles_sam::alignment::io::Write as _;
use noodles_sam::alignment::record::Flags;
use noodles_sam::alignment::record_buf::RecordBuf;
use noodles_sam::header::record::value::{map, Map};
use noodles_sam::header::Header;
use std::io::Cursor;

const R1: u16 = 0x40; // FIRST_SEGMENT
const R2: u16 = 0x80; // LAST_SEGMENT
const SUPP: u16 = 0x800; // SUPPLEMENTARY

/// Build a BAM in memory with no @HD SO tag — mirrors `bwa-mem2` output.
fn write_bam(records: &[(&str, u16)]) -> Vec<u8> {
    let hd_map = Map::<map::Header>::default();
    let header = Header::builder().set_header(hd_map).build();

    let mut writer = bam::io::Writer::new(Vec::new());
    writer.write_header(&header).unwrap();
    for (name, flags) in records {
        let mut r = RecordBuf::default();
        *r.name_mut() = Some((*name).into());
        *r.flags_mut() = Flags::from_bits_retain(*flags);
        writer.write_alignment_record(&header, &r).unwrap();
    }
    writer.try_finish().unwrap();
    writer.into_inner().into_inner()
}

#[test]
fn pairs_templates_in_order() {
    let q = write_bam(&[("a", R1), ("a", R2), ("b", R1), ("b", R2)]);
    let b = write_bam(&[("a", R1), ("a", R2), ("b", R1), ("b", R2)]);

    let templates: Vec<_> = template_iter(Cursor::new(q), Cursor::new(b))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();

    assert_eq!(templates.len(), 2);
    assert_eq!(templates[0].name, "a");
    assert_eq!(templates[1].name, "b");
}

#[test]
fn extra_supplementary_within_template_is_tolerated() {
    // query carries an extra supplementary for template "a"; baseline does not.
    // The old lockstep walk errored here; the template walk must pair them.
    let q = write_bam(&[("a", R1), ("a", R2), ("a", R1 | SUPP), ("b", R1)]);
    let b = write_bam(&[("a", R1), ("a", R2), ("b", R1)]);

    let templates: Vec<_> = template_iter(Cursor::new(q), Cursor::new(b))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();

    assert_eq!(templates.len(), 2);
    assert_eq!(templates[0].name, "a");
    assert_eq!(templates[0].query.len(), 3);
    assert_eq!(templates[0].baseline.len(), 2);
}

#[test]
fn unsorted_bams_without_so_tag_are_accepted() {
    let buf = write_bam(&[("a", R1), ("a", R2)]);
    let templates: Vec<_> = template_iter(Cursor::new(buf.clone()), Cursor::new(buf))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    assert_eq!(templates.len(), 1);
    assert_eq!(templates[0].query.len(), 2);
}

#[test]
fn diverging_template_order_errors() {
    let q = write_bam(&[("a", R1), ("b", R1)]);
    let b = write_bam(&[("a", R1), ("c", R1)]);

    let results: Vec<_> = template_iter(Cursor::new(q), Cursor::new(b))
        .unwrap()
        .collect();
    assert!(results[0].is_ok());
    assert!(results[1]
        .as_ref()
        .is_err_and(|e| e.to_string().contains("diverged")));
}

#[test]
fn unequal_template_counts_error() {
    let q = write_bam(&[("a", R1), ("b", R1)]);
    let b = write_bam(&[("a", R1)]);

    let results: Vec<_> = template_iter(Cursor::new(q), Cursor::new(b))
        .unwrap()
        .collect();
    assert!(results[0].is_ok());
    assert!(results[1]
        .as_ref()
        .is_err_and(|e| e.to_string().contains("exhausted")));
}
