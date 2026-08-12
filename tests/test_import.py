"""Sanity test — the package installs cleanly and exposes __version__."""

import ast
from pathlib import Path

import bwa_mem3_bench

_SHA_LEN = 40


def test_version_string() -> None:
    assert bwa_mem3_bench.__version__ == "0.1.0"


def test_minibwa_sha_reads_canonical_pin() -> None:
    """minibwa_sha() returns the 40-char hex pin from build-arg-defaults.env,
    matching the vendored submodule commit."""
    sha = bwa_mem3_bench.minibwa_sha()
    assert len(sha) == _SHA_LEN
    assert all(c in "0123456789abcdef" for c in sha)


def test_holodeck_ref_reads_canonical_pin() -> None:
    """holodeck_ref() returns the fg-labs/holodeck git ref pinned in
    build-arg-defaults.env that the image cargo-installs `holodeck` from."""
    ref = bwa_mem3_bench.holodeck_ref()
    assert len(ref) == _SHA_LEN
    assert all(c in "0123456789abcdef" for c in ref)


def test_holodeck_repo_reads_canonical_pin() -> None:
    """holodeck_repo() returns the fg-labs/holodeck repo URL from
    build-arg-defaults.env, so `cli build` never hardcodes a stale repo."""
    assert bwa_mem3_bench.holodeck_repo() == "https://github.com/fg-labs/holodeck"


def test_no_command_module_is_shadowed_by_its_own_re_export() -> None:
    """A `commands/<name>.py` defining `def <name>()` shadows itself when re-exported.

    `commands/__init__.py` does `from bwa_mem3_bench.commands.<name> import <name>`,
    which binds the FUNCTION to `commands.<name>` — so the attribute no longer
    resolves to the module, and `from bwa_mem3_bench.commands import build as
    build_module` silently hands back a function. The symptom is an
    `AttributeError: 'function' object has no attribute 'platform'` in whatever
    test touched a module attribute, which points nowhere near the cause; it bit
    three separate times before the modules were renamed to `_<name>.py`.

    Underscore-prefixed modules are exempt: the prefix is what breaks the name
    collision. `aws.py` and `bench.py` are legitimately unprefixed because they
    are re-exported as MODULES and define no same-named function.
    """
    commands_dir = Path(bwa_mem3_bench.__file__).parent / "commands"
    offenders = []
    for path in sorted(commands_dir.glob("*.py")):
        stem = path.stem
        if stem.startswith("_"):
            continue
        tree = ast.parse(path.read_text())
        defines_same_name = any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == stem
            for node in tree.body
        )
        if defines_same_name:
            offenders.append(stem)

    assert not offenders, (
        f"commands/{{{','.join(offenders)}}}.py define a top-level function matching "
        "the module name, so re-exporting it shadows the module. Rename to "
        "`_<name>.py` (see commands/_build.py)."
    )
