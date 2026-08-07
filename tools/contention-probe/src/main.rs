//! CLI for the contention probe. See `lib.rs` for what it measures and why.

use std::process::ExitCode;
use std::time::Duration;

use contention_probe::{run, ProbeConfig, ProbeResult};

const DEFAULT_SECONDS: f64 = 10.0;
const DEFAULT_WORKING_SET_MB: usize = 64;
const BYTES_PER_MB: usize = 1024 * 1024;

const USAGE: &str = "\
contention-probe — measure this host's memory access rate under current contention

USAGE:
    contention-probe [OPTIONS]

OPTIONS:
    -s, --seconds <F>            wall-clock budget (default 10)
    -w, --working-set-mb <N>     chain size PER THREAD (default 64)
    -t, --threads <N>            concurrent chains (default: available parallelism)
        --seed <N>               chain permutation seed (default 1)
        --json                   emit JSON instead of a human summary
    -h, --help                   print this help

WHY 64 MB PER THREAD BY DEFAULT
    The chain must not fit in last-level cache, or the probe measures cache
    instead of memory. 64 MB per thread clears the LLC of every instance type
    this project benchmarks, with headroom. Raise it if you add a host with a
    very large L3; lower it only if you have checked ns_per_access still looks
    like DRAM (roughly 60-120 ns uncontended).

INTERPRETING THE OUTPUT
    million_accesses_per_sec is the score: higher means a faster host RIGHT NOW.
    Compare it between runs, and record it next to the instance id so host
    quality can be separated from code change after the fact.

    Use it as a FILTER before using it as a divisor. Treating it as a scale
    factor assumes probe and workload degrade proportionally, which is plausible
    but unverified; treating it as a filter only assumes a bad score means a bad
    host.
";

/// Parsed CLI arguments, or a request to print help.
#[derive(Debug)]
enum Args {
    Help,
    Run { config: ProbeConfig, json: bool },
}

fn parse_args<I: Iterator<Item = String>>(args: I) -> Result<Args, String> {
    let mut seconds = DEFAULT_SECONDS;
    let mut working_set_mb = DEFAULT_WORKING_SET_MB;
    let mut threads = std::thread::available_parallelism().map_or(1, Into::into);
    let mut seed = 1u64;
    let mut json = false;

    let mut args = args.peekable();
    while let Some(arg) = args.next() {
        // Every value-taking flag needs the same "was a value actually there?"
        // check; without it `--threads` at the end of argv silently keeps the
        // default and the operator gets a probe they did not ask for.
        let mut value = || -> Result<String, String> {
            args.next().ok_or_else(|| format!("{arg} requires a value"))
        };
        match arg.as_str() {
            "-h" | "--help" => return Ok(Args::Help),
            "--json" => json = true,
            "-s" | "--seconds" => {
                let raw = value()?;
                seconds = raw
                    .parse()
                    .map_err(|_| format!("--seconds: not a number: {raw}"))?;
                if !(seconds.is_finite() && seconds > 0.0) {
                    return Err(format!(
                        "--seconds must be positive and finite, got {seconds}"
                    ));
                }
            }
            "-w" | "--working-set-mb" => {
                let raw = value()?;
                working_set_mb = raw
                    .parse()
                    .map_err(|_| format!("--working-set-mb: not an integer: {raw}"))?;
                if working_set_mb == 0 {
                    return Err("--working-set-mb must be at least 1".to_string());
                }
            }
            "-t" | "--threads" => {
                let raw = value()?;
                threads = raw
                    .parse()
                    .map_err(|_| format!("--threads: not an integer: {raw}"))?;
                if threads == 0 {
                    return Err("--threads must be at least 1".to_string());
                }
            }
            "--seed" => {
                let raw = value()?;
                seed = raw
                    .parse()
                    .map_err(|_| format!("--seed: not an integer: {raw}"))?;
            }
            other => return Err(format!("unrecognised argument: {other}")),
        }
    }

    Ok(Args::Run {
        config: ProbeConfig {
            duration: Duration::from_secs_f64(seconds),
            working_set_bytes: working_set_mb * BYTES_PER_MB,
            threads,
            seed,
        },
        json,
    })
}

/// JSON shaped to be merged straight into a run's `meta.json`, so a probe score
/// lands beside the `instance_id` it belongs to.
fn as_json(result: &ProbeResult) -> String {
    format!(
        concat!(
            "{{\"probe\":\"memory-chase\",",
            "\"million_accesses_per_sec\":{:.3},",
            "\"ns_per_access\":{:.2},",
            "\"accesses\":{},",
            "\"elapsed_s\":{:.3},",
            "\"threads\":{},",
            "\"working_set_bytes_per_thread\":{}}}"
        ),
        result.million_accesses_per_sec(),
        result.ns_per_access(),
        result.accesses,
        result.elapsed.as_secs_f64(),
        result.threads,
        result.working_set_bytes,
    )
}

fn as_human(result: &ProbeResult) -> String {
    format!(
        "score  {:.3} M accesses/s   (higher = faster host right now)\n\
         latency {:.2} ns/access      (expect ~60-120 ns uncontended DRAM)\n\
         {} threads x {} MB chain, {} accesses in {:.2} s",
        result.million_accesses_per_sec(),
        result.ns_per_access(),
        result.threads,
        result.working_set_bytes / BYTES_PER_MB,
        result.accesses,
        result.elapsed.as_secs_f64(),
    )
}

fn main() -> ExitCode {
    match parse_args(std::env::args().skip(1)) {
        Ok(Args::Help) => {
            print!("{USAGE}");
            ExitCode::SUCCESS
        }
        Ok(Args::Run { config, json }) => {
            let result = run(&config);
            if json {
                println!("{}", as_json(&result));
            } else {
                println!("{}", as_human(&result));
            }
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("error: {message}\n");
            eprint!("{USAGE}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Args, String> {
        parse_args(args.iter().map(|s| (*s).to_string()))
    }

    fn config_of(args: &[&str]) -> ProbeConfig {
        match parse(args) {
            Ok(Args::Run { config, .. }) => config,
            other => panic!("expected a run, got {other:?}"),
        }
    }

    #[test]
    fn defaults_are_a_usable_probe() {
        let config = config_of(&[]);
        assert!(config.duration.as_secs_f64() > 0.0);
        assert_eq!(
            config.working_set_bytes,
            DEFAULT_WORKING_SET_MB * BYTES_PER_MB
        );
        assert!(config.threads >= 1);
    }

    #[test]
    fn flags_override_every_default() {
        let config = config_of(&["--seconds", "0.5", "-w", "8", "-t", "3", "--seed", "77"]);
        assert!((config.duration.as_secs_f64() - 0.5).abs() < 1e-9);
        assert_eq!(config.working_set_bytes, 8 * BYTES_PER_MB);
        assert_eq!(config.threads, 3);
        assert_eq!(config.seed, 77);
    }

    #[test]
    fn a_flag_missing_its_value_is_an_error() {
        // Not a default: a truncated command line must fail loudly rather than
        // silently probe with settings the operator did not choose.
        for args in [
            vec!["--seconds"],
            vec!["--working-set-mb"],
            vec!["--threads"],
            vec!["--seed"],
        ] {
            let err = parse(&args).expect_err("expected an error");
            assert!(err.contains("requires a value"), "{err}");
        }
    }

    #[test]
    fn zero_and_negative_settings_are_rejected() {
        // Each of these would produce a probe that reports a number without
        // measuring anything: no time, no working set, no threads.
        for args in [
            vec!["--seconds", "0"],
            vec!["--seconds", "-1"],
            vec!["--working-set-mb", "0"],
            vec!["--threads", "0"],
        ] {
            assert!(parse(&args).is_err(), "{args:?} should have been rejected");
        }
    }

    #[test]
    fn nonsense_values_are_rejected() {
        assert!(parse(&["--seconds", "soon"]).is_err());
        assert!(parse(&["--threads", "many"]).is_err());
        assert!(parse(&["--nope"]).is_err());
    }

    #[test]
    fn help_is_requested_not_run() {
        assert!(matches!(parse(&["--help"]), Ok(Args::Help)));
        assert!(matches!(parse(&["-h"]), Ok(Args::Help)));
    }

    #[test]
    fn json_is_flat_and_carries_the_score() {
        let result = ProbeResult {
            accesses: 1_000_000,
            elapsed: Duration::from_secs(1),
            threads: 2,
            working_set_bytes: 4 * BYTES_PER_MB,
        };
        let json = as_json(&result);
        // Flat and quoted so it merges into meta.json without reshaping.
        assert!(json.starts_with('{') && json.ends_with('}'));
        assert!(
            json.contains("\"million_accesses_per_sec\":1.000"),
            "{json}"
        );
        assert!(json.contains("\"threads\":2"), "{json}");
        assert!(json.contains("\"accesses\":1000000"), "{json}");
    }

    #[test]
    fn human_output_names_both_metrics() {
        let result = ProbeResult {
            accesses: 500_000,
            elapsed: Duration::from_secs(1),
            threads: 1,
            working_set_bytes: BYTES_PER_MB,
        };
        let text = as_human(&result);
        assert!(text.contains("M accesses/s"), "{text}");
        assert!(text.contains("ns/access"), "{text}");
    }
}
