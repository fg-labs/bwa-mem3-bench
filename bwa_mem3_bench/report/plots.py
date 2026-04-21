"""Plotly helpers: bar + trend plots, PNG export via kaleido."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px


def bar_by_arch(  # noqa: PLR0913
    df: pd.DataFrame,
    *,
    y: str,
    title: str,
    y_title: str,
    out_png: Path,
    color: str = "arch",
    facet: str | None = "sample",
) -> None:
    """Bar plot grouped by arch (color) and faceted by sample."""
    fig = px.bar(
        df,
        x="arch",
        y=y,
        color=color,
        facet_col=facet,
        title=title,
        labels={y: y_title, "arch": "Architecture"},
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.write_image(str(out_png), width=1200, height=500)


def trend_over_commits(  # noqa: PLR0913
    df: pd.DataFrame,
    *,
    y: str,
    title: str,
    y_title: str,
    out_png: Path,
    color: str = "arch",
) -> None:
    """Line plot: `y` over `fg_labs_sha` (assumed ordered), one line per `color`."""
    fig = px.line(
        df,
        x="fg_labs_sha",
        y=y,
        color=color,
        markers=True,
        title=title,
        labels={y: y_title, "fg_labs_sha": "fg-labs SHA"},
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.write_image(str(out_png), width=1200, height=500)
