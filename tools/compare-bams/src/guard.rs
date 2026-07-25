//! Fail loudly when the observed aux-tag set is not the one the config declared.
//!
//! `ignore_tags` decides which tag differences count against concordance. That
//! makes the tag policy load-bearing on the headline number, and a policy can be
//! wrong in two directions:
//!
//! * a tag nobody anticipated starts appearing, or
//! * an `ignore_tags` entry names a tag that is not there at all.
//!
//! The first is already visible in the score — an unanticipated tag lands in the
//! strict set, diverges on ~100% of reads, and concordance collapses — but the
//! score does not say *which* tag did it, so the run reads as an unexplained
//! 40-point drop. This guard names it.
//!
//! The second is invisible. An `ignore_tags` entry matching nothing suppresses
//! nothing, so the score is unchanged and no report field moves. That is exactly
//! the shape of bench #34, where `compare_options.ignore_tags` looked like it
//! filtered tags, read as though it did, and was wired to nothing at all. The
//! principle that fell out of that work — **config that silently does nothing is
//! the bug** — is what this check enforces.
//!
//! Both checks run once, at finalize, over the whole-run tallies in
//! [`ConcordanceReport::by_tag`]. They have to: "0 of 9,980,872 records" is not a
//! statement any single record can make, and reporting every deviation at once
//! beats failing on the first one found.

use serde::{Deserialize, Serialize};

use crate::config::CompareOptions;
use crate::report::ConcordanceReport;

/// One way the observed tag set deviated from what the config declared.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum TagGuardViolation {
    /// A tag was observed that is on neither `expect_tags` nor `ignore_tags`.
    ///
    /// Carries the per-side presence counts because they distinguish the two
    /// causes: a tag on both sides is a new tag both aligners emit (bump the
    /// allowlist), while a tag on one side only is a divergence between them
    /// (decide whether it is comparable before allowing it).
    UnexpectedTag {
        tag: String,
        query_present: u64,
        baseline_present: u64,
    },
    /// An `ignore_tags` entry matched no record on either side.
    DeadIgnoreEntry { tag: String, total_reads: u64 },
}

impl TagGuardViolation {
    /// A one-line operator-facing rendering, naming the tag and what to do.
    #[must_use]
    pub fn message(&self) -> String {
        match self {
            Self::UnexpectedTag {
                tag,
                query_present,
                baseline_present,
            } => format!(
                "unexpected tag {tag:?}: present on {query_present} query and \
                 {baseline_present} baseline records, but listed in neither \
                 expect_tags nor ignore_tags. It is being compared strictly. \
                 Add it to expect_tags if that is correct, or to ignore_tags if \
                 the two sides are not comparable on it."
            ),
            Self::DeadIgnoreEntry { tag, total_reads } => format!(
                "ignore_tags entry {tag:?} matched no record on either side \
                 (0 of {total_reads} primaries). A tag nobody emits cannot be \
                 suppressed, so this entry does nothing. Either the aligner \
                 stopped emitting it, or the entry is dead config."
            ),
        }
    }
}

/// Check the finished report's observed tag set against the declared policy.
///
/// Returns every deviation rather than the first, so one run surfaces the whole
/// list. An empty result means the observed tag set is exactly what the config
/// anticipated.
///
/// The unexpected-tag check is skipped when `expect_tags` is empty: an
/// unconfigured allowlist is indistinguishable from an empty one, and treating
/// it as empty would flag every tag in the file. The workflow closes that hole
/// in config validation, by requiring each comparison kind to declare a
/// non-empty set.
#[must_use]
pub fn check(report: &ConcordanceReport, opts: &CompareOptions) -> Vec<TagGuardViolation> {
    let mut violations = Vec::new();

    if !opts.expect_tags.is_empty() {
        for (tag, counter) in &report.by_tag {
            let observed = counter.query_present > 0 || counter.baseline_present > 0;
            if observed && !opts.expect_tags.contains(tag) && !opts.ignore_tags.contains(tag) {
                violations.push(TagGuardViolation::UnexpectedTag {
                    tag: tag.clone(),
                    query_present: counter.query_present,
                    baseline_present: counter.baseline_present,
                });
            }
        }
    }

    for tag in &opts.ignore_tags {
        if opts.absent_ok_tags.contains(tag) {
            continue;
        }
        let observed = report
            .by_tag
            .get(tag)
            .is_some_and(|c| c.query_present > 0 || c.baseline_present > 0);
        if !observed {
            violations.push(TagGuardViolation::DeadIgnoreEntry {
                tag: tag.clone(),
                total_reads: report.total_reads,
            });
        }
    }

    violations
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::report::TagCounter;

    /// A report whose `by_tag` says each named tag was seen on both sides.
    fn report_with(total_reads: u64, present: &[&str]) -> ConcordanceReport {
        let mut r = ConcordanceReport {
            total_reads,
            ..ConcordanceReport::default()
        };
        for tag in present {
            r.by_tag.insert(
                (*tag).to_string(),
                TagCounter {
                    query_present: total_reads,
                    baseline_present: total_reads,
                    ..TagCounter::default()
                },
            );
        }
        r
    }

    fn opts(ignore: &[&str], expect: &[&str], absent_ok: &[&str]) -> CompareOptions {
        CompareOptions {
            ignore_tags: ignore.iter().map(|s| (*s).to_string()).collect(),
            expect_tags: expect.iter().map(|s| (*s).to_string()).collect(),
            absent_ok_tags: absent_ok.iter().map(|s| (*s).to_string()).collect(),
            ..CompareOptions::default()
        }
    }

    #[test]
    fn a_declared_tag_set_passes() {
        let r = report_with(100, &["NM", "MD", "MQ"]);
        assert!(check(&r, &opts(&["MQ"], &["NM", "MD"], &[])).is_empty());
    }

    /// The headline case: a tag nobody anticipated is named rather than left as
    /// an unexplained drop in `concordance_pct`.
    #[test]
    fn an_unlisted_tag_is_reported_with_its_presence_counts() {
        let r = report_with(100, &["NM", "ZZ"]);
        assert_eq!(
            check(&r, &opts(&[], &["NM"], &[])),
            vec![TagGuardViolation::UnexpectedTag {
                tag: "ZZ".to_string(),
                query_present: 100,
                baseline_present: 100,
            }]
        );
    }

    /// Being on the ignore list is as good as being on the allowlist: the point
    /// is that the tag was anticipated, not which bucket anticipated it.
    #[test]
    fn an_ignored_tag_does_not_also_need_to_be_expected() {
        let r = report_with(100, &["NM", "MQ"]);
        assert!(check(&r, &opts(&["MQ"], &["NM"], &[])).is_empty());
    }

    /// `expect_tags` means MAY appear, not MUST: a listed tag that never shows
    /// up is what lets one list serve samples with legitimately different tag
    /// sets (single-end reads carry no mate tags).
    #[test]
    fn an_expected_tag_that_never_appears_is_not_a_violation() {
        let r = report_with(100, &["NM"]);
        assert!(check(&r, &opts(&[], &["NM", "MC", "MQ"], &[])).is_empty());
    }

    /// The silent class — an ignore entry that suppresses nothing.
    #[test]
    fn a_dead_ignore_entry_is_reported() {
        let r = report_with(9_980_872, &["NM"]);
        assert_eq!(
            check(&r, &opts(&["MQ"], &["NM"], &[])),
            vec![TagGuardViolation::DeadIgnoreEntry {
                tag: "MQ".to_string(),
                total_reads: 9_980_872,
            }]
        );
    }

    /// A tag observed on one side only still counts as observed — that is the
    /// normal state of a cross-tool ignore entry such as `MQ` vs `bwa-mem2`.
    #[test]
    fn one_sided_presence_keeps_an_ignore_entry_alive() {
        let mut r = report_with(100, &["NM"]);
        r.by_tag.insert(
            "MQ".to_string(),
            TagCounter {
                query_present: 100,
                baseline_present: 0,
                ..TagCounter::default()
            },
        );
        assert!(check(&r, &opts(&["MQ"], &["NM"], &[])).is_empty());
    }

    /// `absent_ok_tags` excuses an entry from the audit WITHOUT unignoring it —
    /// the meth `MQ`/`HN` case (fg-labs/bwa-mem3#296).
    #[test]
    fn absent_ok_suppresses_the_dead_entry_finding() {
        let r = report_with(100, &["NM"]);
        assert!(check(&r, &opts(&["MQ", "HN"], &["NM"], &["MQ", "HN"])).is_empty());
        // ...and only for the tags it names.
        assert_eq!(check(&r, &opts(&["MQ", "HN"], &["NM"], &["MQ"])).len(), 1);
    }

    /// An unconfigured allowlist cannot be enforced, so it must not flag every
    /// tag in the file. The dead-entry check still runs — it is self-describing.
    #[test]
    fn an_empty_allowlist_disables_only_the_unexpected_tag_check() {
        let r = report_with(100, &["NM", "ZZ"]);
        let v = check(&r, &opts(&["MQ"], &[], &[]));
        assert_eq!(
            v,
            vec![TagGuardViolation::DeadIgnoreEntry {
                tag: "MQ".to_string(),
                total_reads: 100,
            }]
        );
    }

    /// Every deviation is reported, not just the first, so one run surfaces the
    /// whole list rather than one item per fix-and-rerun cycle.
    #[test]
    fn all_violations_are_reported_together() {
        let r = report_with(100, &["NM", "YY", "ZZ"]);
        let v = check(&r, &opts(&["MQ", "HN"], &["NM"], &[]));
        assert_eq!(v.len(), 4, "{v:?}");
    }
}
