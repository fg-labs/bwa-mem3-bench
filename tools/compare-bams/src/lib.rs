//! Compare two BAMs grouped by template and classify discordances.
//!
//! Entry point: [`compare`], which streams two BAMs one read-pair (template) at
//! a time and returns a [`report::ConcordanceReport`]. The two BAMs are expected
//! to be produced from the same input FASTQ (so templates appear in matching
//! order on both sides); no sort-order header tag is required. Within a template
//! the two sides may carry a different number of supplementary alignments — that
//! is reported, not treated as a fatal divergence.

pub mod classify;
pub mod compare;
pub mod config;
pub mod report;
pub mod template_reader;

pub use classify::{classify, Discordance};
pub use compare::compare;
pub use config::CompareOptions;
pub use report::ConcordanceReport;
pub use template_reader::{template_iter, Template};
