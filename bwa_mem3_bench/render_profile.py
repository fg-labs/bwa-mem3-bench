"""Render the Snakemake AWS Batch profile from its template.

The template lives at ``workflow/profiles/aws-batch.config.yaml.template`` —
deliberately OUTSIDE the ``aws-batch/`` profile directory itself, not just
alongside the rendered file — and contains placeholders for
deployment-specific values (ECR URI, S3 bucket, region, optional cost-center
tag). The rendered ``config.yaml`` is gitignored — each environment renders
its own copy from CDK outputs + env vars before invoking ``snakemake``.

The directory split matters: Snakemake's own ``--profile`` config-file
discovery matches any filename starting with ``config`` + an optional
``.v<N>+`` version qualifier + ``.yaml`` via an UNANCHORED regex
(``config(.v(?P<min_major>\\d+)\\+)?.yaml``, matched with ``re.match`` — no
trailing ``$``), so ``config.yaml.template`` satisfies it as a same-priority
candidate alongside the real ``config.yaml``. Ties are broken by
``os.listdir()`` order, which is filesystem-dependent, not alphabetical —
inside the coordinator's Batch container this consistently favored the
image-baked ``.template`` file (present since image build) over the
freshly-rendered ``config.yaml`` (written at container start), so snakemake
loaded the UNSUBSTITUTED template and failed with a YAML parse error on its
bare ``${COST_CENTER_LINE}`` placeholder line. Keeping the template out of
the profile directory entirely sidesteps the ambiguity regardless of
directory-listing order or future changes to Snakemake's discovery regex.

Required values come from :func:`bwa_mem3_bench.aws_config.load` (which reads
``cdk/outputs.json`` then falls back to env vars). The optional cost-center
tag is read from the ``BWA_MEM3_BENCH_COST_CENTER`` env var; if unset, no
``CostCenter`` tag is emitted.
"""

from __future__ import annotations

import os
from pathlib import Path
from string import Template

from bwa_mem3_bench import REPO_ROOT, aws_config

_PROFILES_DIR = REPO_ROOT / "workflow" / "profiles"
_PROFILE_DIR = _PROFILES_DIR / "aws-batch"
DEFAULT_TEMPLATE = _PROFILES_DIR / "aws-batch.config.yaml.template"
DEFAULT_OUTPUT = _PROFILE_DIR / "config.yaml"


def render_profile(
    *,
    template: Path = DEFAULT_TEMPLATE,
    output: Path = DEFAULT_OUTPUT,
    dry_run: bool = False,
) -> None:
    """Render the Snakemake AWS Batch profile from its template.

    :param template: source template path.
    :param output: rendered profile path. Snakemake reads ``--profile <dir>``,
        so this must be named ``config.yaml`` inside the profile directory.
    :param dry_run: print the rendered content to stdout without writing.
    """
    cfg = aws_config.load()
    if not cfg.ecr_repo_uri:
        raise RuntimeError(
            "ECR repository URI is not set. Run `cdk deploy` to populate "
            "cdk/outputs.json, or set BWA_MEM3_BENCH_ECR_REPO."
        )
    if not cfg.bucket:
        raise RuntimeError(
            "S3 bucket name is not set. Populate cdk/outputs.json or set BWA_MEM3_BENCH_S3_BUCKET."
        )

    cost_center = os.environ.get("BWA_MEM3_BENCH_COST_CENTER", "").strip()
    cost_center_line = f"  CostCenter: {cost_center}\n" if cost_center else ""

    text = Template(template.read_text()).substitute(
        ECR_REPO_URI=cfg.ecr_repo_uri,
        AWS_REGION=cfg.region,
        S3_BUCKET=cfg.bucket,
        COST_CENTER_LINE=cost_center_line,
    )

    if dry_run:
        print(f"# would write {len(text)} bytes to {output}\n")
        print(text)
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    print(f"rendered {template.name} → {output}")
