"""The package must parse on the image's Python, which is older than the dev env's.

The per-SHA image runs Debian bookworm's ``python3`` (3.11), and in-image snakemake
imports ``bwa_mem3_bench`` from the Snakefile, so syntax newer than ``requires-python``
breaks the coordinator and every native-executor worker at parse time -- while the
pixi env (3.12) and biffix (its own 3.12) keep passing.

The ``ast`` check below is a fast in-suite guard, but ``feature_version`` is
best-effort: on 3.12 it still accepts PEP 701 f-strings (``f"{d["k"]}"``) that
3.11 rejects. The authoritative check is ``pixi run -e py311 py-floor-parse``,
which compiles the package with a real 3.11 interpreter; the tests here pin that
it stays wired into ``pixi run check`` and CI. Neither covers ``workflow/Snakefile``
or ``workflow/rules/*.smk``: the image's 3.11 compiles those too, but they are
snakemake syntax, so checking them would need snakemake in the ``py311`` env.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text())
_PIXI = tomllib.loads((_ROOT / "pixi.toml").read_text())
_FLOOR = (3, 11)


def test_tool_targets_match_the_requires_python_floor() -> None:
    """ruff and mypy target the floor, so lint never rewrites code into newer syntax."""
    assert _PYPROJECT["project"]["requires-python"] == ">=3.11"
    assert _PYPROJECT["tool"]["ruff"]["target-version"] == "py311"
    assert _PYPROJECT["tool"]["mypy"]["python_version"] == "3.11"


def test_floor_interpreter_check_is_wired_in() -> None:
    """A real 3.11 parse runs in `pixi run check` and in CI, not only the ast guard."""
    feature = _PIXI["feature"]["py311"]
    assert feature["dependencies"]["python"] == "3.11.*"
    assert feature["tasks"]["py-floor-parse"] == (
        "python scripts/check-python-floor.py bwa_mem3_bench workflow/reference-indexing"
    )
    assert _PIXI["environments"]["py311"]["features"] == ["py311"]
    assert {"task": "py-floor-parse", "environment": "py311"} in _PIXI["tasks"]["check"][
        "depends-on"
    ]
    # Parsed, not grepped: a commented-out step still matches a text search.
    workflow = yaml.safe_load((_ROOT / ".github" / "workflows" / "check.yml").read_text())
    steps = workflow["jobs"]["python-and-rust"]["steps"]
    gate = [step for step in steps if step.get("run") == "pixi run -e py311 py-floor-parse"]
    assert gate, "python-and-rust job has no step running py-floor-parse"
    # A disabled or non-fatal step still matches on `run`, so pin that it gates the job.
    assert "if" not in gate[0], "py-floor-parse step must run unconditionally"
    assert not gate[0].get("continue-on-error", False), "py-floor-parse step must fail the job"


def _load_floor_script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import scripts/check-python-floor.py with its floor set to the running interpreter.

    The real floor is 3.11, which the dev env is not, so the script would refuse to run
    here. Retargeting the floor exercises its compile/report logic on any interpreter;
    the 3.11 grammar itself is exercised by the `py-floor-parse` task.
    """
    path = _ROOT / "scripts" / "check-python-floor.py"
    spec = importlib.util.spec_from_file_location("check_python_floor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_FLOOR", sys.version_info[:2])
    return module


def test_floor_script_passes_a_clean_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module that compiles yields exit status 0."""
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert _load_floor_script(monkeypatch).main([str(tmp_path)]) == 0


def test_floor_script_rejects_a_syntax_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A module that fails to compile yields 1 and is named on stderr.

    Checking stderr for the file matters: the script also returns 1 for a version
    mismatch or an empty root, so the status alone does not show this path ran.
    """
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    assert _load_floor_script(monkeypatch).main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert f"{tmp_path / 'bad.py'}:1:" in captured.err
    assert "ok.py" not in captured.err
    version = sys.version.split()[0]
    assert captured.out == f"compiled 2 modules with Python {version}: 1 failed\n"


def test_floor_script_refuses_the_wrong_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run on anything but the floor, the script fails rather than passing vacuously."""
    (tmp_path / "ok.py").write_text("x = 1\n")
    module = _load_floor_script(monkeypatch)
    monkeypatch.setattr(module, "_FLOOR", (2, 7))
    assert module.main([str(tmp_path)]) == 1
    assert "expected Python 2.7" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path",
    sorted((_ROOT / "bwa_mem3_bench").rglob("*.py")),
    ids=lambda p: str(p.relative_to(_ROOT)),
)
def test_module_parses_at_the_floor(path: Path) -> None:
    """Every package module parses with the 3.11 grammar (best-effort; see the module doc)."""
    ast.parse(path.read_text(), filename=str(path), feature_version=_FLOOR)
