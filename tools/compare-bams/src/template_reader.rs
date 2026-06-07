//! Stream two BAMs grouped by template (QNAME), in matching template order.
//!
//! Records for a template are assumed **contiguous** — the order both
//! `bwa-mem2` and `bwa-mem3` emit when run over the same input FASTQ. Only one
//! template's records are buffered at a time, never the whole file, so memory is
//! O(largest single template) regardless of BAM size.
//!
//! Template-level lockstep re-establishes synchronization at the granularity of
//! a read pair: the two BAMs must traverse the same templates in the same order
//! (else iteration errors). *Within* a template the two sides may carry a
//! different number of records — e.g. `bwa-mem3` emits supplementary alignments
//! that upstream `bwa-mem2 v2.2.1` does not — which is exactly what this module
//! exists to tolerate (the old record-by-record walk treated that as fatal).

use std::io::Read;

use anyhow::{anyhow, Context, Result};
use noodles_bam as bam;
use noodles_bgzf as bgzf;
use noodles_sam::alignment::record_buf::RecordBuf;

/// All records sharing one QNAME, from each BAM.
#[derive(Debug)]
pub struct Template {
    pub name: String,
    pub query: Vec<RecordBuf>,
    pub baseline: Vec<RecordBuf>,
}

/// The record's QNAME, or an error if it is absent.
///
/// QNAME is mandatory in SAM/BAM, and every record bwa-mem2/bwa-mem3 emits — mapped
/// or unmapped — carries one. Erroring (rather than grouping name-less records under
/// a shared empty key) keeps the O(largest single template) memory guarantee: a
/// malformed BAM full of name-less records cannot silently accumulate into one
/// unbounded group.
fn name_of(r: &RecordBuf) -> Result<String> {
    r.name()
        .map(std::string::ToString::to_string)
        .ok_or_else(|| anyhow!("BAM record is missing its mandatory QNAME field"))
}

/// Adapts a record stream into contiguous same-QNAME groups using a single
/// record of look-ahead. Generic over the record source so the grouping logic
/// is unit-testable without BAM I/O.
pub struct Grouper<I: Iterator<Item = Result<RecordBuf>>> {
    inner: I,
    peeked: Option<RecordBuf>,
    done: bool,
}

impl<I: Iterator<Item = Result<RecordBuf>>> Grouper<I> {
    pub fn new(inner: I) -> Self {
        Self {
            inner,
            peeked: None,
            done: false,
        }
    }

    /// Pull the next record, draining the look-ahead slot first.
    fn pull(&mut self) -> Result<Option<RecordBuf>> {
        if let Some(r) = self.peeked.take() {
            return Ok(Some(r));
        }
        self.inner.next().transpose()
    }
}

impl<I: Iterator<Item = Result<RecordBuf>>> Iterator for Grouper<I> {
    type Item = Result<(String, Vec<RecordBuf>)>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.done {
            return None;
        }
        let first = match self.pull() {
            Ok(Some(r)) => r,
            Ok(None) => {
                self.done = true;
                return None;
            }
            Err(e) => {
                self.done = true;
                return Some(Err(e));
            }
        };
        let name = match name_of(&first) {
            Ok(n) => n,
            Err(e) => {
                self.done = true;
                return Some(Err(e));
            }
        };
        let mut group = vec![first];
        loop {
            match self.pull() {
                Ok(Some(r)) => match name_of(&r) {
                    Ok(n) if n == name => group.push(r),
                    Ok(_) => {
                        self.peeked = Some(r); // first record of the next group
                        break;
                    }
                    Err(e) => {
                        self.done = true;
                        return Some(Err(e));
                    }
                },
                Ok(None) => break,
                Err(e) => {
                    self.done = true;
                    return Some(Err(e));
                }
            }
        }
        Some(Ok((name, group)))
    }
}

/// Zips two QNAME-grouped streams, enforcing matching template order.
pub struct Templates<Q, B>
where
    Q: Iterator<Item = Result<(String, Vec<RecordBuf>)>>,
    B: Iterator<Item = Result<(String, Vec<RecordBuf>)>>,
{
    query: Q,
    baseline: B,
    done: bool,
}

impl<Q, B> Templates<Q, B>
where
    Q: Iterator<Item = Result<(String, Vec<RecordBuf>)>>,
    B: Iterator<Item = Result<(String, Vec<RecordBuf>)>>,
{
    pub fn new(query: Q, baseline: B) -> Self {
        Self {
            query,
            baseline,
            done: false,
        }
    }
}

impl<Q, B> Iterator for Templates<Q, B>
where
    Q: Iterator<Item = Result<(String, Vec<RecordBuf>)>>,
    B: Iterator<Item = Result<(String, Vec<RecordBuf>)>>,
{
    type Item = Result<Template>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.done {
            return None;
        }
        let q = match self.query.next() {
            Some(Ok(g)) => Some(g),
            Some(Err(e)) => {
                self.done = true;
                return Some(Err(e));
            }
            None => None,
        };
        let b = match self.baseline.next() {
            Some(Ok(g)) => Some(g),
            Some(Err(e)) => {
                self.done = true;
                return Some(Err(e));
            }
            None => None,
        };
        match (q, b) {
            (None, None) => {
                self.done = true;
                None
            }
            (Some((qn, _)), None) => {
                self.done = true;
                Some(Err(anyhow!(
                    "BAM streams diverged: query has template '{qn}' but baseline is exhausted \
                     — the two BAMs cover different read sets"
                )))
            }
            (None, Some((bn, _))) => {
                self.done = true;
                Some(Err(anyhow!(
                    "BAM streams diverged: baseline has template '{bn}' but query is exhausted \
                     — the two BAMs cover different read sets"
                )))
            }
            (Some((qn, qr)), Some((bn, br))) => {
                if qn == bn {
                    Some(Ok(Template {
                        name: qn,
                        query: qr,
                        baseline: br,
                    }))
                } else {
                    self.done = true;
                    Some(Err(anyhow!(
                        "BAM streams diverged: query template='{qn}', baseline template='{bn}' \
                         — both BAMs must emit templates in the same (input) order"
                    )))
                }
            }
        }
    }
}

/// A streaming `Iterator<Item = Result<RecordBuf>>` over a BAM's records.
fn record_stream<R: Read>(inner: R) -> Result<impl Iterator<Item = Result<RecordBuf>>> {
    let mut reader: bam::io::Reader<bgzf::Reader<R>> = bam::io::Reader::new(inner);
    let header = reader.read_header().context("reading BAM header")?;
    Ok(std::iter::from_fn(move || {
        let mut rec = RecordBuf::default();
        match reader.read_record_buf(&header, &mut rec) {
            Ok(0) => None,
            Ok(_) => Some(Ok(rec)),
            Err(e) => Some(Err(anyhow::Error::from(e))),
        }
    }))
}

/// Stream two BAMs as templates (read-pair groups) in matching order.
///
/// # Errors
///
/// Returns an error if either BAM header cannot be read, if the two BAMs cover
/// different templates / template order, or on any underlying read error.
pub fn template_iter<R1: Read, R2: Read>(
    query: R1,
    baseline: R2,
) -> Result<impl Iterator<Item = Result<Template>>> {
    let q = Grouper::new(record_stream(query)?);
    let b = Grouper::new(record_stream(baseline)?);
    Ok(Templates::new(q, b))
}

#[cfg(test)]
mod tests {
    use super::*;
    use noodles_sam::alignment::record::Flags;

    /// Minimal record carrying just a name + flags — enough for grouping/order tests.
    fn named(name: &str, flags: u16) -> RecordBuf {
        RecordBuf::builder()
            .set_name(name)
            .set_flags(Flags::from_bits_retain(flags))
            .build()
    }

    fn ok_stream(recs: Vec<RecordBuf>) -> impl Iterator<Item = Result<RecordBuf>> {
        recs.into_iter().map(Ok)
    }

    #[test]
    fn groups_contiguous_records_by_qname() {
        let recs = vec![
            named("a", 0x40),
            named("a", 0x80),
            named("a", 0x840), // supplementary of a
            named("b", 0x40),
            named("b", 0x80),
        ];
        let groups: Vec<_> = Grouper::new(ok_stream(recs)).map(Result::unwrap).collect();
        assert_eq!(groups.len(), 2);
        assert_eq!(groups[0].0, "a");
        assert_eq!(groups[0].1.len(), 3);
        assert_eq!(groups[1].0, "b");
        assert_eq!(groups[1].1.len(), 2);
    }

    #[test]
    fn templates_pair_up_matching_names_with_uneven_record_counts() {
        // query has an extra supplementary for template "a"; still pairs by name.
        let q = Grouper::new(ok_stream(vec![
            named("a", 0x40),
            named("a", 0x80),
            named("a", 0x840),
            named("b", 0x40),
        ]));
        let b = Grouper::new(ok_stream(vec![
            named("a", 0x40),
            named("a", 0x80),
            named("b", 0x40),
        ]));
        let ts: Vec<_> = Templates::new(q, b).map(Result::unwrap).collect();
        assert_eq!(ts.len(), 2);
        assert_eq!(ts[0].name, "a");
        assert_eq!(ts[0].query.len(), 3);
        assert_eq!(ts[0].baseline.len(), 2);
    }

    #[test]
    fn record_without_qname_is_an_error() {
        // A name-less record must error rather than group under an empty key
        // (which would defeat the O(largest-template) memory guarantee).
        let unnamed = RecordBuf::builder()
            .set_flags(Flags::from_bits_retain(0x40))
            .build();
        let mut grouper = Grouper::new(ok_stream(vec![unnamed]));
        let first = grouper.next().expect("one item");
        assert!(first
            .unwrap_err()
            .to_string()
            .contains("missing its mandatory QNAME"));
    }

    #[test]
    fn template_order_divergence_is_an_error() {
        let q = Grouper::new(ok_stream(vec![named("a", 0x40), named("b", 0x40)]));
        let b = Grouper::new(ok_stream(vec![named("a", 0x40), named("c", 0x40)]));
        let results: Vec<_> = Templates::new(q, b).collect();
        assert!(results[0].is_ok());
        assert!(results[1].is_err());
        assert!(results[1]
            .as_ref()
            .unwrap_err()
            .to_string()
            .contains("diverged"));
    }

    #[test]
    fn unequal_template_counts_is_an_error() {
        let q = Grouper::new(ok_stream(vec![named("a", 0x40), named("b", 0x40)]));
        let b = Grouper::new(ok_stream(vec![named("a", 0x40)]));
        let results: Vec<_> = Templates::new(q, b).collect();
        assert!(results[0].is_ok());
        assert!(results[1].is_err());
        assert!(results[1]
            .as_ref()
            .unwrap_err()
            .to_string()
            .contains("exhausted"));
    }
}
