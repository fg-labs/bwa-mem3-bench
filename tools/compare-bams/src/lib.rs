//! Compare two BAMs in lockstep and classify discordances.
//!
//! Entry point: [`compare`], which streams two BAMs record-by-record and
//! returns a [`report::ConcordanceReport`]. The two BAMs are expected to be
//! produced from the same input FASTQ (so reads appear in matching order on
//! both sides); no sort-order header tag is required.

pub mod classify;
pub mod compare;
pub mod config;
pub mod pair_reader;
pub mod report;

// Re-exports are added as later tasks populate each module.
pub use classify::{classify, Discordance}; // Task 3
pub use compare::compare; // Task 5
pub use config::CompareOptions; // Task 3
pub use pair_reader::{pair_iter, Pair}; // Task 4
pub use report::ConcordanceReport; // Task 5
