"""Capture IMDS metadata — written alongside Snakemake's benchmark: timing.tsv.

Host attribution is the point of this rule: without it, a cross-run timing
difference cannot be told apart from a cross-HOST difference. That distinction
is not academic — the 394f8f8 sweep showed an apparent +7.5% c6a regression that
bare-metal reproduction disproved (main is 2.2-4.5% FASTER at every SIMD tier),
and with no recorded host identity there was no way to attribute the Batch
delta to anything.
"""


rule emit_meta:
    output:
        meta = "runs/{sha}/{sample}/{arch}/rep-{rep}/benchmarks/meta.json",
    shell:
        # IMDSv2. The previous version issued a tokenless IMDSv1 GET, which
        # AL2023 rejects; `curl -s` ignores the HTTP status and still exits 0,
        # so the `|| echo local` fallback never fired and an EMPTY STRING was
        # written. Every meta.json in the project's history has
        # instance_type="" and availability_zone="" as a result.
        #
        # Verified on a live c6a.4xlarge:
        #   IMDSv1 (tokenless):  value=[]  curl_exit=0
        #   IMDSv2 (with token): c6a.4xlarge   i-0638a349a50d9df56
        #
        # Two fixes: request a session token first, and use `curl -sf` so an
        # HTTP error is a non-zero exit and the fallback actually works.
        #
        # NOTE: a container reaches IMDS through an extra network hop, so the
        # instance needs HttpPutResponseHopLimit >= 2 for the token PUT to
        # succeed from inside the task. If TOKEN comes back empty on real Batch
        # workers, that is the cause and the fix belongs in cdk/ (launch
        # template), not here. The rule degrades to "unknown" rather than
        # failing the job either way — metadata is diagnostic, not load-bearing.
        r"""
        mkdir -p $(dirname {output.meta})
        IMDS=http://169.254.169.254/latest
        TOKEN=$(curl -sf -X PUT --max-time 2 "$IMDS/api/token" \
                  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)
        imds_get() {{
            if [ -n "$TOKEN" ]; then
                curl -sf --max-time 2 -H "X-aws-ec2-metadata-token: $TOKEN" \
                    "$IMDS/meta-data/$1" || echo unknown
            else
                echo unknown
            fi
        }}
        INSTANCE_TYPE=$(imds_get instance-type)
        AZ=$(imds_get placement/availability-zone)
        # instance-id is the field that actually enables attribution: it is what
        # tells you two reps shared a host (and therefore contended) rather than
        # merely running in the same AZ.
        INSTANCE_ID=$(imds_get instance-id)
        KERNEL=$(uname -r)
        # JSON emitted via python stdlib — a heredoc would need `<<-EOF` with
        # tab indentation that snakemake's shell-body dedenting does not preserve.
        export INSTANCE_TYPE AZ INSTANCE_ID KERNEL
        python3 -c '
import json, os, sys
json.dump({{
    "fg_labs_sha": "{wildcards.sha}",
    "sample": "{wildcards.sample}",
    "arch": "{wildcards.arch}",
    "rep": int("{wildcards.rep}"),
    "instance_type": os.environ["INSTANCE_TYPE"],
    "availability_zone": os.environ["AZ"],
    "instance_id": os.environ["INSTANCE_ID"],
    "kernel": os.environ["KERNEL"],
}}, sys.stdout)
' > {output.meta}
        """
