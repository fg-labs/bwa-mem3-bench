"""Standalone reference indexing, without cloud access or a container runtime."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflow" / "reference-indexing"
SNAKEFILE = WORKFLOW / "Snakefile"
REQUIRED = (".amb", ".ann", ".bwt.2bit.64", ".pac")
METADATA = (".version.txt", ".source.sha256", ".checksums.sha256")
MIB = 1024 * 1024

# Run the real S3 storage plugin with every remote call redirected to a local directory, so
# storage staging, upload ordering and output checks run end to end without network access.
FAKE_S3 = """
import os, shutil, sys
from pathlib import Path
import snakemake_storage_plugin_s3 as s3

root = Path(os.environ["FAKE_S3_ROOT"])

def remote(self):
    return root / self.bucket / self.key

def retrieve(self):
    self.local_path().parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(remote(self), self.local_path())

def store(self):
    remote(self).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(self.local_path(), remote(self))

async def inventory(self, cache):
    return None

s3.StorageProvider.__post_init__ = lambda self: None
s3.StorageObject.inventory = inventory
s3.StorageObject.bucket_exists = lambda self: True
s3.StorageObject.is_dir = lambda self: False
s3.StorageObject.exists = lambda self: remote(self).exists()
s3.StorageObject.mtime = lambda self: remote(self).stat().st_mtime
s3.StorageObject.size = lambda self: remote(self).stat().st_size
s3.StorageObject.retrieve_object = retrieve
s3.StorageObject.store_object = store
s3.StorageObject.remove = lambda self: remote(self).unlink(missing_ok=True)

from snakemake.cli import main
sys.argv[0] = "snakemake"
main()
"""


def helper() -> ModuleType:
    """Load the workflow-local validation module without changing import paths."""
    spec = importlib.util.spec_from_file_location(
        "reference_indexing", WORKFLOW / "reference_indexing.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((WORKFLOW / "config.schema.json").read_text())
    return loaded


def schema_accepts(config: dict[str, Any]) -> bool:
    return jsonschema.Draft7Validator(schema()).is_valid(config)


def run_workflow(
    work: Path, config: dict[str, object], *args: str
) -> subprocess.CompletedProcess[str]:
    """Run the actual DAG in an isolated directory with an explicit config."""
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config))
    return subprocess.run(
        [
            str(Path(sys.executable).with_name("snakemake")),
            "--snakefile",
            str(SNAKEFILE),
            "--directory",
            str(work),
            "--configfile",
            str(config_path),
            "--cores",
            "16",
            "--printshellcmds",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def fasta(tmp_path: Path) -> Path:
    """A read-only FASTA under a shell-sensitive source path."""
    source = tmp_path / "source space ' $(touch INJECTED)"
    source.mkdir()
    path = source / "assembly.v1.fasta"
    path.write_text(">chr1\nACGTNNACGTACGTACGT\n")
    path.chmod(0o444)
    return path


@pytest.fixture
def indexer(tmp_path: Path) -> Path:
    """A deterministic executable implementing the indexer's CLI contract."""
    binary = tmp_path / "fake indexer"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "mode = os.environ.get('INDEX_TEST_MODE', '')\n"
        "if args == ['version']:\n"
        "    if mode == 'version-failure':\n"
        "        print('fake version failed', file=sys.stderr); sys.exit(2)\n"
        "    if mode != 'version-empty':\n"
        "        print('fake-indexer 1.0')\n"
        "    sys.exit(0)\n"
        "assert args[0] == 'index'\n"
        "assert args[args.index('-t') + 1] == '16'\n"
        "assert int(args[args.index('--max-memory') + 1]) > 0\n"
        "prefix = args[args.index('-p') + 1]\n"
        "assert pathlib.Path(prefix).parent != pathlib.Path(args[-1]).parent\n"
        "for suffix in ['.amb', '.ann', '.bwt.2bit.64', '.pac']:\n"
        "    if mode == 'partial' and suffix == '.pac': continue\n"
        "    pathlib.Path(prefix + suffix).write_text('' if mode == 'empty' else suffix)\n"
        "if '--emit-unpacked-ref' in args:\n"
        "    pathlib.Path(prefix + '.0123').write_text('unpacked')\n"
        "print('index build complete', flush=True)\n"
        "if mode == 'failure':\n"
        "    print('fake index failed', file=sys.stderr); sys.exit(7)\n"
    )
    binary.chmod(0o755)
    return binary


def expected_names(fasta_name: str, unpacked: bool = False) -> set[str]:
    suffixes = (*REQUIRED, *((".0123",) if unpacked else ()), *METADATA, ".index.log")
    return {fasta_name + suffix for suffix in suffixes}


def check_assembly_directory(directory: Path, fasta: Path, unpacked: bool = False) -> None:
    """Every artifact present, nonempty and checksummed; the source digest names the FASTA."""
    expected = expected_names(fasta.name, unpacked)
    assert {p.name for p in directory.iterdir()} == expected
    assert all(p.stat().st_size > 0 for p in directory.iterdir())
    assert "fake-indexer 1.0" in (directory / (fasta.name + ".version.txt")).read_text()
    assert "index build complete" in (directory / (fasta.name + ".index.log")).read_text()
    records = (directory / (fasta.name + ".checksums.sha256")).read_text().splitlines()
    checksums = {line.split("  ")[1]: line.split("  ")[0] for line in records}
    assert set(checksums) == expected - {fasta.name + ".checksums.sha256"}
    for name, digest in checksums.items():
        assert digest == hashlib.sha256((directory / name).read_bytes()).hexdigest()
    source = (directory / (fasta.name + ".source.sha256")).read_text()
    assert source == f"{hashlib.sha256(fasta.read_bytes()).hexdigest()}  {fasta.name}\n"


def test_dotted_assembly_names(tmp_path: Path, fasta: Path) -> None:
    assemblies = {
        "GRCh38.p6": "s3://input-bucket/GRCh38.p6/genome.fa",
        "hg38": "s3://input-bucket/hg38/assembly.fasta",
        "mm10": "/data/mm10/genome.fa",
        "b37_v1-2": str(fasta),
    }
    assert helper().validate_config({"assemblies": assemblies}).assemblies == assemblies
    result = run_workflow(tmp_path / "run", {"assemblies": {"GRCh38.p6": str(fasta)}}, "-n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "indexes/GRCh38.p6/assembly.v1.fasta.bwt.2bit.64" in result.stdout + result.stderr


@pytest.mark.parametrize("reference", ["/data/ref.fa", "s3://input-bucket/a/ref.fasta"])
def test_defaults_and_preserved_basename(reference: str) -> None:
    config = helper().validate_config({"assemblies": {"hg38": reference}})
    assert config.threads == 16  # noqa: PLR2004
    assert config.mem_mb == 98304  # noqa: PLR2004
    assert config.binary == "bwa-mem2.fg-labs"
    assert not config.emit_unpacked_ref
    assert config.assemblies == {"hg38": reference}
    assert helper().fasta_basename(reference) == reference.rsplit("/", 1)[1]


def test_schema_defaults_and_names_match_validator() -> None:
    properties = schema()["properties"]
    validated = helper().validate_config({"assemblies": {"r": "/d/r.fa"}})
    for key, default in helper().DEFAULTS.items():
        assert properties[key]["default"] == default == getattr(validated, key)
    assert set(properties) == {"assemblies", *helper().DEFAULTS}
    name_pattern = properties["assemblies"]["propertyNames"]["pattern"]
    assert name_pattern == f"^{helper().NAME_PATTERN}$"


BAD_PATHS = {
    "relative.fa": "absolute local paths or s3://bucket/key",
    "/data/../ref.fa": "components",
    "/data/./ref.fa": "components",
    "/data//ref.fa": "components",
    "s3://bucket/a/../../ref.fa": "components",
    "s3://bucket//ref.fa": "components",
    "s3://bucket/ref.fa?versionId=x": "control characters",
    "s3://bucket/ref.fa#fragment": "control characters",
    "/data/%2e%2e/ref.fa": "control characters",
    "/data/a\\b.fa": "control characters",
    "/data/ref\t.fa": "control characters",
    "/data/ref\n.fa": "control characters",
    "/data/{sample}.fa": "control characters",
    "s3:///ref.fa": "bucket name",
    "s3://Bucket/ref.fa": "bucket name",
    "S3://bucket/ref.fa": "absolute local paths or s3://bucket/key",
    "https://example.com/ref.fa": "absolute local paths or s3://bucket/key",
    "/data/ref.fa.gz": "<name>.fa",
    "/data/.fa": "<name>.fa",
    "s3://bucket/.fasta": "<name>.fa",
    "/data/": "components",
}
GOOD_PATHS = [
    "/ref.fa",
    "/data/with space/ref.fasta",
    "/data/..ref.fa",
    "s3://my.bucket-1/refs/GRCh38.p6.fa",
]


@pytest.mark.parametrize(("reference", "message"), BAD_PATHS.items())
def test_rejects_bad_paths_in_validator_and_schema(reference: str, message: str) -> None:
    with pytest.raises(ValueError, match=f"assembly 'ref': .*{message}"):
        helper().validate_config({"assemblies": {"ref": reference}})
    assert not schema_accepts({"assemblies": {"ref": reference}})


@pytest.mark.parametrize("reference", GOOD_PATHS)
def test_accepts_good_paths_in_validator_and_schema(reference: str) -> None:
    assert helper().validate_config({"assemblies": {"ref": reference}}).assemblies
    assert schema_accepts({"assemblies": {"ref": reference}})


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "nonempty map"),
        ({"assemblies": {}}, "nonempty map"),
        ({"assemblies": []}, "nonempty map"),
        ({"assemblies": {"ref": 42}}, "nonempty string"),
        ({"assemblies": {"ref": ""}}, "nonempty string"),
        ({"assemblies": {"hg38": "/a/x.fa", "HG38": "/b/x.fa"}}, "differ only in case"),
        ({"assemblies": {"r": "/d/r.fa"}, "threads": 0}, "threads must be a positive"),
        ({"assemblies": {"r": "/d/r.fa"}, "threads": True}, "threads must be a positive"),
        ({"assemblies": {"r": "/d/r.fa"}, "threads": "16"}, "threads must be a positive"),
        ({"assemblies": {"r": "/d/r.fa"}, "mem_mb": -1}, "mem_mb must be a positive"),
        ({"assemblies": {"r": "/d/r.fa"}, "mem_mb": False}, "mem_mb must be a positive"),
        ({"assemblies": {"r": "/d/r.fa"}, "emit_unpacked_ref": "false"}, "boolean"),
        ({"assemblies": {"r": "/d/r.fa"}, "emit_unpacked_ref": 1}, "boolean"),
        ({"assemblies": {"r": "/d/r.fa"}, "unknown_option": True}, "unknown config keys"),
        ({"assemblies": {"r": "/d/r.fa"}, 1: True}, "unknown config keys"),
    ],
)
def test_rejects_bad_configuration(config: dict[Any, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        helper().validate_config(config)


@pytest.mark.parametrize("name", ["..", ".", "-rf", "_x", "a/b", "a b", "", 1])
def test_rejects_bad_assembly_names(name: object) -> None:
    with pytest.raises(ValueError, match="must match"):
        helper().validate_config({"assemblies": {name: "/data/ref.fa"}})
    if isinstance(name, str):
        assert not schema_accepts({"assemblies": {name: "/data/ref.fa"}})


@pytest.mark.parametrize("binary", ["", "indexer --meth", "bin/indexer", "{x}", "/a\nb", "a\tb"])
def test_rejects_bad_binary_in_validator_and_schema(binary: str) -> None:
    with pytest.raises(ValueError, match="binary must be"):
        helper().validate_config({"assemblies": {"r": "/d/r.fa"}, "binary": binary})
    assert not schema_accepts({"assemblies": {"r": "/d/r.fa"}, "binary": binary})


@pytest.mark.parametrize("binary", ["bwa-mem2.fg-labs", "/opt/my tools/bwa-mem3"])
def test_accepts_good_binary_in_validator_and_schema(binary: str) -> None:
    assert helper().validate_config({"assemblies": {"r": "/d/r.fa"}, "binary": binary}).binary
    assert schema_accepts({"assemblies": {"r": "/d/r.fa"}, "binary": binary})


@pytest.mark.parametrize(
    ("mem_mb", "budget_mib"),
    [(98304, 98304 - 4915), (131072, 131072 - 6553), (32000, 32000 - 2048), (3000, 1500)],
)
def test_memory_budget_leaves_the_indexer_reserve(mem_mb: int, budget_mib: int) -> None:
    assert helper().index_memory_budget_bytes(mem_mb) == budget_mib * MIB


@pytest.mark.parametrize("unpacked", [False, True])
def test_complete_index_outputs(tmp_path: Path, fasta: Path, indexer: Path, unpacked: bool) -> None:
    work = tmp_path / "run space"
    config = {
        "assemblies": {"test-ref": str(fasta)},
        "binary": str(indexer),
        "emit_unpacked_ref": unpacked,
    }
    result = run_workflow(work, config)
    assert result.returncode == 0, result.stdout + result.stderr
    check_assembly_directory(work / "indexes" / "test-ref", fasta, unpacked)
    assert list(fasta.parent.iterdir()) == [fasta]
    assert not (work / "INJECTED").exists()
    assert not (work / "logs").exists()
    rerun = run_workflow(work, config, "--dry-run")
    assert rerun.returncode == 0
    assert "Nothing to be done" in rerun.stdout + rerun.stderr


# Each failure mode of the fake indexer, and the text its build log must keep.
FAILURE_LOGS = {
    "failure": "fake index failed",
    "empty": "index build complete",
    "partial": "index build complete",
    "version-failure": "fake version failed",
    "version-empty": "Building standard reference index",
}


@pytest.mark.parametrize("mode", FAILURE_LOGS)
def test_failures_fail_the_job_and_keep_the_log(
    tmp_path: Path, fasta: Path, indexer: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    logged = FAILURE_LOGS[mode]
    monkeypatch.setenv("INDEX_TEST_MODE", mode)
    work = tmp_path / "run"
    result = run_workflow(work, {"assemblies": {"ref": str(fasta)}, "binary": str(indexer)})
    assert result.returncode != 0
    directory = work / "indexes" / "ref"
    assert logged in (directory / (fasta.name + ".index.log")).read_text()
    for suffix in (*REQUIRED, *METADATA):
        assert not (directory / (fasta.name + suffix)).exists(), suffix


def test_dry_run_resources_multiple_basenames(tmp_path: Path, fasta: Path) -> None:
    other = tmp_path / "other.fa"
    other.write_text(fasta.read_text())
    config = {"assemblies": {"one": str(fasta), "two": str(other)}, "threads": 8, "mem_mb": 131072}
    result = run_workflow(tmp_path / "run", config, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "threads: 8" in output
    assert "mem_mb=131072" in output
    assert "indexes/one/assembly.v1.fasta.bwt.2bit.64" in output
    assert "indexes/two/other.fa.bwt.2bit.64" in output
    assert "-t 8" in output
    assert f"--max-memory {(131072 - 6553) * MIB} " in output
    assert "--meth" not in output
    assert "-u " not in output
    override = run_workflow(
        tmp_path / "override",
        config,
        "--dry-run",
        "--set-resources",
        "index_reference:mem_mb=32000",
    )
    assert override.returncode == 0, override.stdout + override.stderr
    assert f"--max-memory {(32000 - 2048) * MIB} " in override.stdout + override.stderr


def test_invalid_config_fails_before_dag(tmp_path: Path) -> None:
    result = run_workflow(
        tmp_path, {"assemblies": {"../escape": "s3://bucket/ref.fa"}}, "--dry-run"
    )
    assert result.returncode != 0
    assert "Invalid reference-indexing config" in result.stdout + result.stderr
    assert "'../escape'" in result.stdout + result.stderr


def run_with_fake_s3(
    work: Path, fake_s3: Path, config: dict[str, object], *args: str
) -> subprocess.CompletedProcess[str]:
    """Run the workflow with S3 default storage backed by a local directory."""
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / "storage-config.json"
    config_path.write_text(json.dumps(config))
    return subprocess.run(
        [
            sys.executable,
            "-c",
            FAKE_S3,
            "--snakefile",
            str(SNAKEFILE),
            "--directory",
            str(work),
            "--configfile",
            str(config_path),
            "--cores",
            "16",
            "--printshellcmds",
            "--default-storage-provider",
            "s3",
            "--default-storage-prefix",
            "s3://output-bucket/run-123",
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "FAKE_S3_ROOT": str(fake_s3)},
    )


@pytest.mark.parametrize("remote_input", [True, False])
def test_storage_mapping_dry_run(tmp_path: Path, fasta: Path, remote_input: bool) -> None:
    """Remote inputs stay at their URI, local inputs stay local, outputs map to the run prefix."""
    fake_s3 = tmp_path / "fake-s3"
    remote = fake_s3 / "source-bucket" / "references" / fasta.name
    remote.parent.mkdir(parents=True)
    remote.write_bytes(fasta.read_bytes())
    reference = f"s3://source-bucket/references/{fasta.name}" if remote_input else str(fasta)
    result = run_with_fake_s3(tmp_path, fake_s3, {"assemblies": {"ref": reference}}, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    for suffix in (*REQUIRED, *METADATA, ".index.log"):
        assert "output-bucket/run-123/indexes/ref/assembly.v1.fasta" + suffix in output
    if remote_input:
        assert "source-bucket/references/assembly.v1.fasta" in output
        assert "output-bucket/run-123/s3:" not in output
    else:
        assert str(fasta) in output
        assert "output-bucket/run-123/" + str(fasta) not in output
        assert "output-bucket/run-123" + str(fasta) not in output


def test_storage_run_uploads_every_artifact(tmp_path: Path, fasta: Path, indexer: Path) -> None:
    """A real run under S3 default storage succeeds and uploads a verifiable assembly directory."""
    fake_s3 = tmp_path / "fake-s3"
    remote = fake_s3 / "source-bucket" / "references" / fasta.name
    remote.parent.mkdir(parents=True)
    remote.write_bytes(fasta.read_bytes())
    config = {
        "assemblies": {"ref": f"s3://source-bucket/references/{fasta.name}"},
        "binary": str(indexer),
    }
    result = run_with_fake_s3(tmp_path / "run", fake_s3, config)
    assert result.returncode == 0, result.stdout + result.stderr
    directory = fake_s3 / "output-bucket" / "run-123" / "indexes" / "ref"
    check_assembly_directory(directory, remote)
    verify = subprocess.run(
        ["sha256sum", "-c", fasta.name + ".checksums.sha256"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_manifest_paths_stay_inside_the_workflow_bundle() -> None:
    manifest = yaml.safe_load((WORKFLOW / "biffix.yaml").read_text())
    assert manifest["engine"] == "snakemake"
    assert manifest["entrypoint"] == "Snakefile"
    for field in ("entrypoint", "schema", "about"):
        path = WORKFLOW / manifest[field]
        assert path.is_file()
        assert path.resolve().is_relative_to(WORKFLOW.resolve())
    assert "image" not in manifest and "images" not in manifest
    assert schema()["additionalProperties"] is False
    example = yaml.safe_load((WORKFLOW / "config.example.yaml").read_text())
    assert helper().validate_config(example).threads == 16  # noqa: PLR2004


def test_tested_snakemake_matches_the_manifest_engine() -> None:
    """The sm9 test environment pins exactly the engine biffix runs this workflow on."""
    manifest = yaml.safe_load((WORKFLOW / "biffix.yaml").read_text())
    pixi = tomllib.loads((ROOT / "pixi.toml").read_text())
    pin = pixi["feature"]["sm9"]["dependencies"]["snakemake-minimal"]
    assert pin == f"=={manifest['engine_version']}"
