//! Measure how fast *this* host's memory subsystem is right now.
//!
//! # Why this exists
//!
//! Benchmark wall times on shared cloud hosts are not comparable across runs.
//! Measured on this project's own fleet (2026-08-06, `wgs-5M`/m7i, one binary,
//! one input): repeating a replicate on the SAME EC2 instance reproduces within
//! **2.03%**, while letting each replicate pick a fresh instance spreads
//! **9.40%** — and on one sample the same unchanged binary measured 18.89 s and
//! 25.01 s depending only on which machine it landed on.
//!
//! That variance is not lost CPU. Across every replicate `cpu_time / wall` held
//! at 14.1–14.7 of 16 vCPUs, so the scheduler was giving us our cores; what
//! changed was that identical work burned ~20% more CPU-seconds. Cores were
//! stalling on memory, because co-tenants on the same socket were consuming
//! last-level cache and DRAM bandwidth that the instance contract never
//! promised us.
//!
//! So the probe measures **memory access rate under whatever contention exists
//! at this moment**, and nothing else.
//!
//! # Why a pointer chase, and why several threads
//!
//! The probe has to be sensitive to the same resource the workload is sensitive
//! to. bwa-mem's FMI search is a dependent chain of random accesses over a
//! multi-gigabyte index — latency- and memory-parallelism-bound, not
//! ALU-bound. A tight arithmetic loop in registers would read the same on a
//! contended host as on an idle one and tell you nothing.
//!
//! A pointer chase reproduces that shape: each step's address comes from the
//! previous step's value, so the CPU cannot prefetch and cannot overlap the
//! misses within a thread. Running one chain per thread then reproduces the
//! aggregate memory parallelism of a `-t 16` alignment, which is what a noisy
//! neighbour actually degrades.
//!
//! # Why it must not resemble the code under test
//!
//! The probe is deliberately independent of bwa-mem3. If it shared code, then
//! dividing a measurement by a probe score would cancel real regressions along
//! with host noise. `cpu_time` is the cautionary example: it is already a
//! contention signal, but a genuinely slower binary also burns more of it, so
//! normalising by it would flatten everything including the thing you are
//! looking for. A separate pointer chase cannot move when bwa-mem3 changes, and
//! does move when the host degrades — which is exactly the property that makes
//! it usable as a control.
//!
//! # Does it actually respond to a neighbour?
//!
//! Yes, measured. On an otherwise quiet 12-core host, a 4-thread probe against
//! the same 4-thread probe with an 8-thread memory neighbour beside it:
//!
//! ```text
//!   alone                    28.4 / 30.3 / 31.7 M accesses/s   126-141 ns
//!   8-thread neighbour       18.0 / 17.2 / 17.9 M accesses/s   222-233 ns
//!   alone again              29.8 / 29.7 M accesses/s          134 ns
//! ```
//!
//! A 42% drop, tight within each condition (4.7% spread contended), and it
//! recovers when the neighbour leaves — so the score tracks conditions now
//! rather than drifting. The response is larger than the ~20% wall-time effect
//! it is meant to detect, which is the right way round for a detector.
//!
//! Two caveats that shape how it should be read. It is not a *pure* memory
//! probe: 8 spinning CPU-only threads with no memory traffic still cost ~18%,
//! so the score conflates memory contention with CPU availability. For deciding
//! "is this host degraded" that is harmless — either cause means it is — but it
//! matters if the score is ever used as a divisor. And the probe needs a quiet
//! baseline to calibrate against: an early attempt here read 12-22 M/s purely
//! because a build was finishing in the background, which inverted the result
//! until the machine was actually idle.
//!
//! # Filter first, normalise only once validated
//!
//! Treating the score as a *scale factor* assumes probe and workload degrade
//! proportionally. That is plausible and unverified. Treating it as a *filter*
//! only assumes a bad score means a bad host, which is far weaker. Collect
//! scores next to timings, join on the recorded instance id, and check how much
//! of the between-host variance the score explains before dividing by it.

use std::hint::black_box;
use std::time::{Duration, Instant};

/// Bytes per cache line on every architecture this project benchmarks
/// (x86-64 and Graviton alike).
const CACHE_LINE_BYTES: usize = 64;

/// Steps between clock reads. Large enough that `Instant::now` is negligible
/// against the work, small enough to stop close to the requested duration.
const BATCH_STEPS: u64 = 1 << 14;

/// One `u32` slot per cache line, so consecutive chain entries never share a
/// line and every hop is a distinct line.
const SLOTS_PER_LINE: usize = CACHE_LINE_BYTES / std::mem::size_of::<u32>();

/// How the probe should be run.
#[derive(Debug, Clone, Copy)]
pub struct ProbeConfig {
    /// Wall-clock budget. The probe stops at the first batch boundary past it,
    /// so it slightly overshoots rather than truncating a batch.
    pub duration: Duration,
    /// Chain size per thread. Must exceed this host's per-core share of
    /// last-level cache or the chase measures cache, not memory.
    pub working_set_bytes: usize,
    /// Independent chains walked concurrently. Match the alignment's thread
    /// count to load memory the same way the real workload does.
    pub threads: usize,
    /// Seeds the chain permutation. Fixed by default so two runs on one host
    /// walk the same order and differ only by conditions.
    pub seed: u64,
}

/// What the probe measured.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ProbeResult {
    pub accesses: u64,
    pub elapsed: Duration,
    pub threads: usize,
    pub working_set_bytes: usize,
}

impl ProbeResult {
    /// Aggregate dependent-load throughput, in millions of accesses per second.
    ///
    /// This is the headline number: higher is a faster host *right now*. It is
    /// the quantity to compare between two runs, or to divide by.
    #[must_use]
    pub fn million_accesses_per_sec(&self) -> f64 {
        let secs = self.elapsed.as_secs_f64();
        if secs <= 0.0 {
            return 0.0;
        }
        #[allow(clippy::cast_precision_loss)] // f64 is exact to 2^53 accesses
        let accesses = self.accesses as f64;
        accesses / secs / 1e6
    }

    /// Mean latency of one dependent load, in nanoseconds, per thread.
    ///
    /// Reported because it is the number with a physical meaning you can sanity
    /// check: an uncontended DRAM round trip is roughly 60–120 ns, so a reading
    /// far below that means the working set fit in cache and the probe measured
    /// the wrong thing.
    #[must_use]
    pub fn ns_per_access(&self) -> f64 {
        if self.accesses == 0 {
            return 0.0;
        }
        #[allow(clippy::cast_precision_loss)]
        let accesses = self.accesses as f64;
        #[allow(clippy::cast_precision_loss)]
        let threads = self.threads as f64;
        self.elapsed.as_secs_f64() * 1e9 * threads / accesses
    }
}

/// Deterministic xorshift64\*. Hand-rolled to keep the crate dependency-free —
/// the probe gets baked into a benchmark image, so every avoided dependency is
/// one fewer thing that can change what it measures between releases.
struct XorShift64(u64);

impl XorShift64 {
    fn new(seed: u64) -> Self {
        // Zero is a fixed point of xorshift; substitute an arbitrary constant.
        Self(if seed == 0 {
            0x9E37_79B9_7F4A_7C15
        } else {
            seed
        })
    }

    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
}

/// Build a chain that visits every slot exactly once before repeating.
///
/// Sattolo's algorithm, which differs from Fisher-Yates in drawing `j` from
/// `[0, i)` rather than `[0, i]`. That one-element change is what guarantees a
/// SINGLE cycle of full length instead of an arbitrary permutation. It matters
/// here: an arbitrary permutation decomposes into several short cycles, and a
/// walker entering a short one would revisit a handful of lines that then sit
/// in cache, quietly turning a memory probe into a cache probe.
fn build_chain(len: usize, seed: u64) -> Vec<u32> {
    let mut chain: Vec<u32> = (0..len)
        .map(|i| u32::try_from(i).expect("chain length is bounded by u32::MAX"))
        .collect();
    let mut rng = XorShift64::new(seed);
    for i in (1..len).rev() {
        let j = usize::try_from(rng.next_u64() % (i as u64)).expect("j < i fits usize");
        chain.swap(i, j);
    }
    chain
}

/// Number of chain slots for a given working set, rounded to whole cache lines.
///
/// At least two slots, because a one-slot chain is a self-loop that never
/// leaves L1 and would report an absurdly fast host.
#[must_use]
pub fn slots_for(working_set_bytes: usize) -> usize {
    let lines = working_set_bytes / CACHE_LINE_BYTES;
    (lines * SLOTS_PER_LINE).max(2)
}

/// Walk one chain until `deadline`, returning how many hops were made.
fn chase(chain: &[u32], deadline: Instant) -> u64 {
    let mut index = 0usize;
    let mut accesses = 0u64;
    while Instant::now() < deadline {
        for _ in 0..BATCH_STEPS {
            index = chain[index] as usize;
        }
        // The chase is pure computation with no observable effect, so without
        // this the optimiser is free to delete the whole loop.
        black_box(index);
        accesses += BATCH_STEPS;
    }
    accesses
}

/// Run the probe.
///
/// Chains are built before the clock starts, so allocation and permutation cost
/// is excluded from the measurement.
#[must_use]
pub fn run(config: &ProbeConfig) -> ProbeResult {
    let threads = config.threads.max(1);
    let slots = slots_for(config.working_set_bytes);
    // Per-thread seeds, so threads walk different orders and cannot
    // accidentally share a hot prefix.
    let chains: Vec<Vec<u32>> = (0..threads)
        .map(|t| build_chain(slots, config.seed ^ (u64::try_from(t).unwrap_or(0) << 32)))
        .collect();

    let start = Instant::now();
    let deadline = start + config.duration;
    let accesses: u64 = std::thread::scope(|scope| {
        let handles: Vec<_> = chains
            .iter()
            .map(|chain| scope.spawn(move || chase(chain, deadline)))
            .collect();
        handles.into_iter().map(|h| h.join().unwrap_or(0)).sum()
    });

    ProbeResult {
        accesses,
        elapsed: start.elapsed(),
        threads,
        working_set_bytes: slots / SLOTS_PER_LINE * CACHE_LINE_BYTES,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tiny(threads: usize) -> ProbeConfig {
        ProbeConfig {
            duration: Duration::from_millis(20),
            working_set_bytes: 1 << 20,
            threads,
            seed: 42,
        }
    }

    #[test]
    fn chain_is_a_single_full_length_cycle() {
        // The property Sattolo buys us. A multi-cycle permutation would let a
        // walker orbit a small subset that then lives in cache, so this is the
        // test that keeps the probe measuring memory.
        let len = 4096;
        let chain = build_chain(len, 7);
        let mut seen = vec![false; len];
        let mut index = 0usize;
        for _ in 0..len {
            assert!(
                !seen[index],
                "revisited slot {index} before covering the chain"
            );
            seen[index] = true;
            index = chain[index] as usize;
        }
        assert_eq!(index, 0, "chain must close back to its start");
        assert!(
            seen.iter().all(|&s| s),
            "every slot must be visited exactly once"
        );
    }

    #[test]
    fn chain_is_a_permutation() {
        let len = 1024;
        let mut sorted = build_chain(len, 99);
        sorted.sort_unstable();
        let expected: Vec<u32> = (0..u32::try_from(len).unwrap()).collect();
        assert_eq!(sorted, expected);
    }

    #[test]
    fn chain_never_maps_a_slot_to_itself() {
        // A fixed point is a self-loop: the walker would spin on one cache line
        // and report a host far faster than it is.
        let chain = build_chain(2048, 5);
        for (i, &next) in chain.iter().enumerate() {
            assert_ne!(next as usize, i, "slot {i} is a self-loop");
        }
    }

    #[test]
    fn same_seed_builds_the_same_chain() {
        // Two probes on one host must differ only by conditions, never by which
        // order they happened to walk.
        assert_eq!(build_chain(512, 1234), build_chain(512, 1234));
        assert_ne!(build_chain(512, 1234), build_chain(512, 5678));
    }

    #[test]
    fn slots_are_one_per_cache_line() {
        assert_eq!(slots_for(64 * 64), 64 * SLOTS_PER_LINE);
        // Degenerate sizes must not produce a self-looping chain.
        assert!(slots_for(0) >= 2);
        assert!(slots_for(1) >= 2);
    }

    #[test]
    fn probe_reports_positive_throughput() {
        let result = run(&tiny(1));
        assert!(result.accesses > 0, "probe made no accesses");
        assert!(result.million_accesses_per_sec() > 0.0);
        assert!(result.ns_per_access() > 0.0);
    }

    #[test]
    fn accesses_scale_with_thread_count() {
        // Not a latency assertion — just that every thread does work, so a
        // multi-threaded probe is not silently running on one core.
        let one = run(&tiny(1)).accesses;
        let four = run(&tiny(4)).accesses;
        assert!(
            four > one,
            "4 threads made {four} accesses vs 1 thread {one}"
        );
    }

    #[test]
    fn zero_threads_is_treated_as_one() {
        let result = run(&tiny(0));
        assert_eq!(result.threads, 1);
        assert!(result.accesses > 0);
    }

    #[test]
    fn empty_result_metrics_do_not_divide_by_zero() {
        let empty = ProbeResult {
            accesses: 0,
            elapsed: Duration::ZERO,
            threads: 1,
            working_set_bytes: 0,
        };
        assert!((empty.million_accesses_per_sec() - 0.0).abs() < f64::EPSILON);
        assert!((empty.ns_per_access() - 0.0).abs() < f64::EPSILON);
    }
}
