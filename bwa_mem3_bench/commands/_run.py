"""Shared subprocess runner with dry-run support."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd: list[str], *, dry_run: bool, cwd: Path | None = None) -> None:
    """Print or execute a command.

    Dry-run prints `[dry-run] cd <cwd> && <cmd>` if `cwd` is set, else
    `[dry-run] <cmd>`. Real run prints `running: <cmd>` to stderr then
    subprocess.run(..., check=True).
    """
    printable = " ".join(shlex.quote(c) for c in cmd)
    prefix = f"cd {cwd} && " if cwd is not None else ""
    if dry_run:
        print(f"[dry-run] {prefix}{printable}")
        return
    print(f"running: {prefix}{printable}", file=sys.stderr)
    subprocess.run(cmd, cwd=cwd, check=True)
