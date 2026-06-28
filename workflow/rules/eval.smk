"""Truth-based accuracy evaluation via `holodeck eval`.

Grades an aligner's BAM directly against holodeck simulation truth — no variant
caller and no methylation extractor in the path, so the numbers isolate the
aligner. One `holodeck eval` invocation emits all three accuracy axes for a
(sample, arch, rep, tool) cell:

  - `<prefix>.eval.txt`     placement + MAPQ calibration (golden-BAM truth)
  - `<prefix>.variants.tsv` per-read variant representation + per-class AS/MAPQ
                            honesty + MD/NM concordance (truth-VCF driven)
  - `<prefix>.meth.tsv`     per-CpG methylation-level correlation vs cpg-truth
                            (meth samples only; an empty file is written for
                            non-meth so the declared output always exists)

The `tool` wildcard selects which aligner's BAM is graded:
  - `fg-labs`  -> the timed fg-labs bwa-mem3 BAM under runs/ (the collapsed and
                  genomic D3 arms are distinct *samples* sharing one source, so
                  each has its own fg-labs BAM).
  - `baseline` -> the baseline cache (bwa-mem2 upstream for non-meth, bwameth
                  for meth).
  - `minibwa`  -> the minibwa cache (bisulfite-aware via `map --meth` for the
                  meth arms, plain `map` otherwise).

`holodeck eval` loads no FMI, but it does take the reference FASTA + `.fai` via
`--reference` (to recompute each read's genomic edits for bisulfite-aware NM/MD
concordance) plus the golden + mapped BAM readers — modest memory. It runs on
the same arch/queue as the alignment it consumes (the BAM is arch-independent,
but pinning eval to the alignment's arch keeps the dependency on one worker
family).
"""


def _truth_inputs(wc) -> list[str]:
    """S3-relative paths to the truth artifacts for `wc.sample`.

    Co-located under the sample's `source` prefix: `golden.bam` (placement +
    variant-footprint truth) and `truth.vcf` (the injected SNVs eval scores),
    plus `cpg-truth.bedGraph` for meth samples (the methylation-level truth).
    Returned bucket-relative so the S3 storage plugin stages them before the
    shell body runs.
    """
    src = CONFIG.samples[wc.sample].source
    inputs = [f"{src}golden.bam", f"{src}truth.vcf"]
    if _is_meth_sample(wc.sample):
        inputs.append(f"{src}cpg-truth.bedGraph")
    return inputs


def _eval_query_bam(wc) -> str:
    """The aligner BAM graded for this (tool, sample, arch, rep) cell.

    Each arm caches its BAM under a different prefix, so the `tool` wildcard
    routes to the producing rule's output: fg-labs under runs/, the upstream/
    bwameth baseline under baseline/, minibwa under minibwa/.
    """
    if wc.tool == "fg-labs":
        return f"runs/{wc.sha}/{wc.sample}/{wc.arch}/rep-{wc.rep}/aligned.bam"
    if wc.tool == "baseline":
        return (
            f"baseline/bwa-mem2-{CONFIG.upstream_tag}/{wc.sample}/"
            f"{wc.arch}/rep-{wc.rep}/aligned.bam"
        )
    if wc.tool == "minibwa":
        return (
            f"minibwa/{MINIBWA_SHA}/{wc.sample}/{wc.arch}/rep-{wc.rep}/aligned.minibwa.bam"
        )
    raise ValueError(f"unknown eval tool {wc.tool!r}")


def _eval_reference_inputs(wc) -> list[str]:
    """The reference FASTA + .fai for `holodeck eval --reference`.

    Enables bisulfite-aware genomic NM/MD concordance: holodeck recomputes each
    read's edits against the reference (index-loaded via the .fai) rather than
    comparing convention-dependent NM/MD tags. Index 0 is the plain .fasta.
    """
    sample = CONFIG.samples[wc.sample]
    fasta_name = CONFIG.references[sample.reference]["fasta_name"]
    base = f"references/{sample.reference}/{fasta_name}"
    return [base, f"{base}.fai"]


rule eval_accuracy:
    """Grade one aligner arm's BAM against holodeck truth → accuracy TSVs."""
    input:
        query = _eval_query_bam,
        truth = _truth_inputs,
        reference = _eval_reference_inputs,
    output:
        eval_txt     = "runs/{sha}/{sample}/{arch}/rep-{rep}/eval/{tool}.eval.txt",
        variants_tsv = "runs/{sha}/{sample}/{arch}/rep-{rep}/eval/{tool}.variants.tsv",
        # Written only by `holodeck eval --meth`; for non-meth samples the shell
        # creates an empty placeholder so this declared output always exists
        # (ingest skips empties).
        meth_tsv     = "runs/{sha}/{sample}/{arch}/rep-{rep}/eval/{tool}.meth.tsv",
    resources:
        batch_queue = lambda wc: CONFIG.archs[wc.arch].batch_queue,
        container_image = lambda wc: image_for_arch(wc.arch),
        # No FMI load, but holodeck loads reference contigs on demand for the
        # genomic-edit NM/MD concordance (targeted datasets touch one/few contigs;
        # a genome-wide dataset can pull most of hg38, ~3 GB) plus the golden +
        # mapped BAM readers. 24 GB covers the worst case on both the c6a (32 GB)
        # and m7i (64 GB) hosts.
        mem_mb = 24000,
    params:
        # `--meth --cpg-truth <bedGraph>` for meth samples (input.truth[2] is the
        # bedGraph, present only then); empty otherwise. The f-string indexes
        # [2] only inside the meth branch, so non-meth never hits an IndexError.
        meth_args = lambda wc, input: (
            f"--meth --cpg-truth {input.truth[2]}" if _is_meth_sample(wc.sample) else ""
        ),
    shell:
        # `--variants` requires `--truth` (holodeck guards this); every sim
        # dataset ships both. The output prefix is the shared stem of the three
        # declared outputs (strip `.eval.txt`).
        r"""
        set -euo pipefail
        mkdir -p $(dirname {output.eval_txt})
        prefix="{output.eval_txt}"
        prefix="${{prefix%.eval.txt}}"
        holodeck eval \
            --mapped {input.query} \
            --truth {input.truth[0]} \
            --variants {input.truth[1]} \
            --reference {input.reference[0]} \
            {params.meth_args} \
            --output "$prefix"
        # Guarantee the declared meth output exists even for non-meth samples,
        # where holodeck writes no `.meth.tsv`.
        test -s {output.meth_tsv} || : > {output.meth_tsv}
        """
