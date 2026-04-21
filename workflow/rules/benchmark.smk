"""Capture IMDS metadata — written alongside Snakemake's benchmark: timing.tsv."""


rule emit_meta:
    output:
        meta = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    shell:
        r"""
        mkdir -p $(dirname {output.meta})
        INSTANCE_TYPE=$(curl -s --max-time 1 http://169.254.169.254/latest/meta-data/instance-type || echo local)
        AZ=$(curl -s --max-time 1 http://169.254.169.254/latest/meta-data/placement/availability-zone || echo local)
        KERNEL=$(uname -r)
        # JSON emitted via python stdlib — a heredoc would need `<<-EOF` with
        # tab indentation that snakemake's shell-body dedenting does not preserve.
        export INSTANCE_TYPE AZ KERNEL
        python3 -c '
import json, os, sys
json.dump({{
    "fg_labs_sha": "{wildcards.sha}",
    "sample": "{wildcards.sample}",
    "arch": "{wildcards.arch}",
    "rep": int("{wildcards.rep}"),
    "instance_type": os.environ["INSTANCE_TYPE"],
    "availability_zone": os.environ["AZ"],
    "kernel": os.environ["KERNEL"],
}}, sys.stdout)
' > {output.meta}
        """
