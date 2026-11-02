"""Compile every module under the given roots with THIS interpreter's parser.

Run under the `py311` pixi environment (`pixi run -e py311 py-floor-parse`), so
the parser is the image's Python 3.11 itself rather than a newer Python asked to
imitate it. Nothing is written: no ``__pycache__``, no imports executed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_FLOOR = (3, 11)


def main(roots: list[str]) -> int:
    """Return 0 when every ``*.py`` under ``roots`` compiles, else 1 (listing the failures)."""
    if sys.version_info[:2] != _FLOOR:
        found = ".".join(map(str, sys.version_info[:3]))
        print(f"expected Python {_FLOOR[0]}.{_FLOOR[1]}, running {found}", file=sys.stderr)
        return 1
    paths = sorted(p for root in roots for p in Path(root).rglob("*.py"))
    if not paths:
        print(f"no Python files under {roots}", file=sys.stderr)
        return 1
    failures = 0
    for path in paths:
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:  # 3.11 raises ValueError on NUL bytes
            failures += 1
            line = getattr(e, "lineno", "?")
            print(f"{path}:{line}: {getattr(e, 'msg', e)}", file=sys.stderr)
    print(f"compiled {len(paths)} modules with Python {sys.version.split()[0]}: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
