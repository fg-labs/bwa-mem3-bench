"""`upload-data` — stage FASTQs and references to the S3 bucket."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from bwa_mem3_bench import REPO_ROOT, aws_config
from bwa_mem3_bench.commands._run import run_cmd
from bwa_mem3_bench.data_sources import sample_sources

_DEFAULT_BUCKET = aws_config.load().bucket


def _upload_reference(bucket: str, *, dry_run: bool) -> None:
    """Invoke scripts/upload_reference.sh (wraps aws s3 sync for hs38DH indexes)."""
    script = REPO_ROOT / "scripts/upload_reference.sh"
    run_cmd(["bash", str(script), bucket], dry_run=dry_run)


def _upload_minibwa_index(bucket: str, *, dry_run: bool) -> None:
    """Invoke scripts/upload_reference.sh with the hg38-minibwa selector.

    Used only on the local-only `private/minibwa-bench` branch.
    """
    script = REPO_ROOT / "scripts/upload_reference.sh"
    run_cmd(["bash", str(script), bucket, "hg38-minibwa"], dry_run=dry_run)


def fastq_transform_cmd(src_fq: Path, dst_fq: Path, every_nth: int, trim_5p: int) -> str:
    """Bash pipeline that keeps every Nth FASTQ record and trims its 5' end.

    Keeps records 1, N+1, 2N+1, ... (deterministic), then drops the first
    ``trim_5p`` characters of the SEQ and QUAL lines of each kept record; the
    header and ``+`` lines pass through unchanged. ``every_nth=1, trim_5p=0`` is
    a plain copy. With a trim, a kept read of at most ``trim_5p`` bases fails
    the transform rather than being staged with an empty SEQ, which an aligner
    may reject, and so does a kept record that is cut short (the stream ends
    inside it) or whose SEQ and QUAL lengths differ. Without a trim the records
    pass through unchecked, as they always have. ``pipefail`` makes a failing
    ``gunzip`` abort the pipe instead of uploading a truncated FASTQ.

    The output is written to a ``.partial.XXXXXX`` file unique to this run (``mktemp``
    beside ``dst_fq``, so concurrent stagings of the same file never share one),
    renamed onto ``dst_fq`` only on success -- an atomic replace, so ``dst_fq``
    is always a complete stream -- and deleted if the pipeline or the rename
    fails, with that step's nonzero status preserved. A SIGHUP, SIGINT or SIGTERM
    removes it too, exiting 128 + the signal number. A run killed with SIGKILL can
    leave its own ``.partial`` behind, but never under the name a later run
    writes or reads.
    """
    if every_nth < 1:
        raise ValueError(f"every_nth must be >= 1, got {every_nth}")
    if trim_5p < 0:
        raise ValueError(f"trim_5p must be >= 0, got {trim_5p}")
    # `bad` keeps END from overriding an earlier rule's exit status.
    program = (
        "{ line = (NR - 1) % 4; rec = (NR - 1 - line) / 4; kept = rec % n == 0 } "
        "t > 0 && kept && line == 1 && length($0) <= t { "
        'printf "record %d has no bases left after a %d-base trim\\n", rec + 1, t '
        '> "/dev/stderr"; bad = 1; exit 3 } '
        "t > 0 && kept && line == 1 { seq_len = length($0) } "
        "t > 0 && kept && line == 3 && length($0) != seq_len { "
        'printf "record %d has %d bases but %d qualities\\n", rec + 1, seq_len, length($0) '
        '> "/dev/stderr"; bad = 1; exit 4 } '
        "kept { print (((line == 1 || line == 3) && t > 0) ? substr($0, t + 1) : $0) } "
        "END { if (!bad && t > 0 && kept && NR % 4 != 0) { "
        'printf "record %d is cut short after %d of 4 lines\\n", rec + 1, NR % 4 '
        '> "/dev/stderr"; exit 5 } }'
    )
    template = shlex.quote(f"{dst_fq}.partial.XXXXXX")  # trailing Xs: BSD + GNU mktemp
    # A signal (Ctrl-C, a closed terminal, a scheduler's SIGTERM) removes this
    # run's own temp file. The traps go in before `mktemp`, with `tmp` empty
    # until it names the file, so no signal lands between the two.
    traps = " ".join(
        f"trap 'rm -f \"$tmp\"; exit {128 + signum}' {name};"
        for name, signum in (("HUP", 1), ("INT", 2), ("TERM", 15))
    )
    return (
        f"set -o pipefail; tmp=; {traps} "
        f"tmp=$(mktemp {template}) || exit 1; "
        f"if gunzip -c {shlex.quote(str(src_fq))} | "
        f"mawk -v n={every_nth} -v t={trim_5p} {shlex.quote(program)} | "
        f'gzip > "$tmp" && mv "$tmp" {shlex.quote(str(dst_fq))}; then :; '
        f'else rc=$?; rm -f "$tmp"; exit $rc; fi'
    )


def _staged_path(src_fq: Path, suffix: str) -> Path:
    """``<dir>/<stem><suffix>.fastq.gz`` beside ``src_fq``; never ``src_fq`` itself."""
    for ext in (".fastq.gz", ".fq.gz"):
        if src_fq.name.endswith(ext):
            return src_fq.with_name(src_fq.name[: -len(ext)] + f"{suffix}.fastq.gz")
    raise ValueError(f"expected a .fastq.gz / .fq.gz source, got {src_fq}")


def _is_reusable_stage(staged: Path, src: Path, *, trimmed: bool) -> bool:
    """Whether ``staged`` exists, decompresses whole, and is no older than ``src``;
    if ``trimmed``, also that its records pass the transform's checks.

    The transform publishes its output atomically, but a staged file can also
    arrive by hand (a copy from another host); a truncated one must be rebuilt,
    not uploaded, and so must a trimmed one holding an empty read, a QUAL line
    of the wrong length, or a final record cut short -- any of which a transform
    predating those checks could write. An untrimmed one is not checked:
    rebuilding it would copy the same records back. Decompressing
    checks integrity, not provenance, so a source replaced at the same path after
    staging (newer mtime, as ``make`` judges it) forces a rebuild too. A missing
    source leaves the staged file reusable. The mtime test misses a replacement
    that keeps an older timestamp (``cp -p``, ``aws s3 cp``); delete the staged
    file to force a rebuild then.
    """
    if not staged.exists():
        return False
    if src.exists() and src.stat().st_mtime_ns > staged.stat().st_mtime_ns:
        return False
    # A trimmed stage is held to the transform's record checks: no empty SEQ,
    # QUAL as long as SEQ, and no record cut short at the end of the stream.
    program = (
        "t && NR % 4 == 2 { seq_len = length($0); if (seq_len == 0) exit 1 } "
        "t && NR % 4 == 0 && length($0) != seq_len { exit 1 } "
        "END { if (t && NR % 4 != 0) exit 1 }"
    )
    check = (
        f"set -o pipefail; gunzip -c {shlex.quote(str(staged))} | "
        f"mawk -v t={int(trimmed)} {shlex.quote(program)}"
    )
    return subprocess.run(["bash", "-c", check], capture_output=True, check=False).returncode == 0


def _upload_sample(sample: str, bucket: str, *, dry_run: bool) -> None:
    srcs = sample_sources(bucket)
    if sample not in srcs:
        raise ValueError(f"unknown sample '{sample}'; known: {sorted(srcs)}")
    src = srcs[sample]
    dest = f"s3://{bucket}/{src.dest_prefix}"

    if src.downsample_every_nth is not None or src.trim_5p:
        if src.downsample_every_nth is not None and src.source_r2 is None:
            # Paired-end only: downsample requires both r1 and r2.
            raise ValueError(f"sample '{sample}' has downsample_every_nth but no source_r2")
        n = src.downsample_every_nth or 1
        # The staged name encodes every transform, so a trimmed and an untrimmed
        # sample over the same source never reuse each other's staged file.
        suffix = f".ds{n}" + (f".trim{src.trim_5p}" if src.trim_5p else "")
        pairs = [(src.source_r1, "r1.fq.gz")]
        if src.source_r2 is not None:
            pairs.append((src.source_r2, "r2.fq.gz"))
        staged = [(src_fq, _staged_path(src_fq, suffix), dest_name) for src_fq, dest_name in pairs]
        # Stage every mate before uploading any, so a failed transform leaves the
        # previous pair in S3 whole rather than a new R1 beside an old R2. S3 has no
        # two-key transaction, so a failed R2 *upload* can still leave a mixed pair;
        # re-running reuses both staged files and re-uploads the pair together.
        for src_fq, dst_fq, _ in staged:
            if not _is_reusable_stage(dst_fq, src_fq, trimmed=src.trim_5p > 0):
                run_cmd(
                    ["bash", "-c", fastq_transform_cmd(src_fq, dst_fq, n, src.trim_5p)],
                    dry_run=dry_run,
                )
        for _, dst_fq, dest_name in staged:
            run_cmd(["aws", "s3", "cp", str(dst_fq), dest + dest_name], dry_run=dry_run)
    elif src.source_bam is not None:
        # Single-end: subsample primary reads genome-wide from a coordinate-sorted
        # BAM and convert to one gzipped FASTQ, then upload as r1.fq.gz.
        #   -F 0x900 drops secondary (0x100) + supplementary (0x800) -> primary only
        #           (duplicates 0x400 are intentionally KEPT — real reads to align).
        #   -s 42.<frac> seeds a reproducible subsample (seed 42, matching the
        #           42.013/42.049 downsample convention).
        #   samtools fastq -0 captures unpaired (flag-0) reads — the SBX case.
        if src.subsample_frac is None:
            raise ValueError(f"sample '{sample}' has source_bam but no subsample_frac")
        src.source_r1.parent.mkdir(parents=True, exist_ok=True)
        if not src.source_r1.exists():
            seed_frac = 42 + src.subsample_frac
            cmd = (
                f"set -o pipefail; "
                f"samtools view -h -F 0x900 -s {seed_frac:.6f} "
                f"{shlex.quote(str(src.source_bam))} "
                f"| samtools fastq -0 {shlex.quote(str(src.source_r1))} -"
            )
            run_cmd(["bash", "-c", cmd], dry_run=dry_run)
        run_cmd(["aws", "s3", "cp", str(src.source_r1), dest + "r1.fq.gz"], dry_run=dry_run)
    elif src.source_r2 is not None:
        # Paired-end, no downsample: upload both FASTQs directly.
        run_cmd(["aws", "s3", "cp", str(src.source_r1), dest + "r1.fq.gz"], dry_run=dry_run)
        run_cmd(["aws", "s3", "cp", str(src.source_r2), dest + "r2.fq.gz"], dry_run=dry_run)
    else:
        # Single-end FASTQ: upload r1 only.
        run_cmd(["aws", "s3", "cp", str(src.source_r1), dest + "r1.fq.gz"], dry_run=dry_run)


def upload_data(
    *,
    what: str = "all",
    bucket: str = _DEFAULT_BUCKET,
    dry_run: bool = False,
) -> None:
    """Upload inputs and references to the S3 bucket.

    For `--what references|all`, set the ``REF_ROOT`` env var to the directory
    holding the Broad hg38 bundle (``Homo_sapiens_assembly38.fasta`` plus
    bwa-mem2 indexes); see ``docs/data-setup.md``.

    :param what: one of `all`, `references`, `data`, `minibwa-index`, or a
        single sample name.
    :param bucket: destination S3 bucket. Defaults to the bucket from
        ``cdk/outputs.json`` (or the ``BWA_MEM3_BENCH_S3_BUCKET`` env var).
    :param dry_run: print commands only.
    """
    if what in ("all", "references"):
        _upload_reference(bucket, dry_run=dry_run)
    if what in ("all", "minibwa-index"):
        _upload_minibwa_index(bucket, dry_run=dry_run)
    if what in ("all", "data"):
        for sample in sample_sources(bucket):
            _upload_sample(sample, bucket, dry_run=dry_run)
    elif what not in ("all", "references", "data", "minibwa-index"):
        _upload_sample(what, bucket, dry_run=dry_run)
