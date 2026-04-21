//! Streaming comparison of two BAMs in matching record order.

use std::io::Read;

use anyhow::Result;

use crate::classify::{classify, Discordance};
use crate::config::CompareOptions;
use crate::pair_reader::{pair_iter, Pair};
use crate::report::ConcordanceReport;

/// Stream two BAMs in lockstep and return an aggregated [`ConcordanceReport`].
pub fn compare<R1, R2>(query: R1, baseline: R2, opts: &CompareOptions) -> Result<ConcordanceReport>
where
    R1: Read,
    R2: Read,
{
    let mut report = ConcordanceReport::default();
    for pair in pair_iter(query, baseline)? {
        match pair? {
            Pair::Both {
                query, baseline, ..
            } => {
                let d = classify(&query, &baseline, opts);
                report.record(&d);
            }
            Pair::QueryOnly(_) => report.record(&Discordance::MappedOnlyQuery),
            Pair::BaselineOnly(_) => report.record(&Discordance::MappedOnlyBaseline),
        }
    }
    report.finalize();
    Ok(report)
}
