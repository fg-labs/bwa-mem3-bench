//! Lockstep iterator over two BAMs in matching qname order.
//!
//! Both BAMs are expected to be produced from the same input FASTQ (so reads
//! appear in matching order on both sides). No header sort-order tag is
//! required — neither coordinate nor name-sort. If qnames diverge mid-stream,
//! iteration returns an error.

use std::io::Read;

use anyhow::{anyhow, Context, Result};
use noodles_bam as bam;
use noodles_bgzf as bgzf;
use noodles_sam::alignment::record_buf::RecordBuf;
use noodles_sam::header::Header;

/// One yielded pairing from [`pair_iter`].
#[derive(Debug)]
pub enum Pair {
    /// Both BAMs had a record at this stream position.
    Both {
        name: String,
        query: RecordBuf,
        baseline: RecordBuf,
    },
    /// The query BAM has extra records past the end of the baseline stream.
    QueryOnly(RecordBuf),
    /// The baseline BAM has extra records past the end of the query stream.
    BaselineOnly(RecordBuf),
}

struct BamStream<R: Read> {
    reader: bam::io::Reader<bgzf::Reader<R>>,
    header: Header,
}

impl<R: Read> BamStream<R> {
    fn open(inner: R) -> Result<Self> {
        let mut reader: bam::io::Reader<bgzf::Reader<R>> = bam::io::Reader::new(inner);
        let header = reader.read_header().context("reading BAM header")?;
        Ok(Self { reader, header })
    }

    fn next_record(&mut self) -> Result<Option<RecordBuf>> {
        let mut rec = RecordBuf::default();
        match self.reader.read_record_buf(&self.header, &mut rec)? {
            0 => Ok(None),
            _ => Ok(Some(rec)),
        }
    }
}

fn name_of(r: &RecordBuf) -> String {
    r.name()
        .map(std::string::ToString::to_string)
        .unwrap_or_default()
}

/// Stream two BAMs in lockstep, emitting a [`Pair`] per record position.
///
/// Both BAMs must emit records in the same order; typically both come from
/// running two aligners on the same FASTQ input. Headers are not required
/// to carry any `SO` tag.
pub fn pair_iter<R1, R2>(query: R1, baseline: R2) -> Result<PairIter<R1, R2>>
where
    R1: Read,
    R2: Read,
{
    let q = BamStream::open(query)?;
    let b = BamStream::open(baseline)?;
    Ok(PairIter::<R1, R2> { q, b })
}

/// Iterator returned by [`pair_iter`].
pub struct PairIter<Q: Read, B: Read> {
    q: BamStream<Q>,
    b: BamStream<B>,
}

impl<Q: Read, B: Read> Iterator for PairIter<Q, B> {
    type Item = Result<Pair>;

    fn next(&mut self) -> Option<Self::Item> {
        let q = match self.q.next_record() {
            Ok(v) => v,
            Err(e) => return Some(Err(e)),
        };
        let b = match self.b.next_record() {
            Ok(v) => v,
            Err(e) => return Some(Err(e)),
        };
        match (q, b) {
            (None, None) => None,
            (Some(q), None) => Some(Ok(Pair::QueryOnly(q))),
            (None, Some(b)) => Some(Ok(Pair::BaselineOnly(b))),
            (Some(q), Some(b)) => {
                let q_name = name_of(&q);
                let b_name = name_of(&b);
                if q_name == b_name {
                    Some(Ok(Pair::Both {
                        name: q_name,
                        query: q,
                        baseline: b,
                    }))
                } else {
                    Some(Err(anyhow!(
                        "BAM streams diverged: query='{q_name}', baseline='{b_name}' \
                         — both BAMs must emit records in the same order"
                    )))
                }
            }
        }
    }
}
