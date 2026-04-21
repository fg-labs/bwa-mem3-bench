"""Markdown table helpers."""

from __future__ import annotations

from collections.abc import Sequence


def md_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    float_fmt: str = "{:.2f}",
) -> str:
    """Return a GitHub-flavored markdown table string."""

    def _cell(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, float):
            return float_fmt.format(v)
        return str(v)

    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(lines)
