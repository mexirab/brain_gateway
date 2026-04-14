"""
Grid layout helpers.

Grafana grid is 24 columns wide. Height units are ~30px each at default zoom.
Panels need `id` (unique int) and `gridPos` (h, w, x, y). These helpers
assign both so dashboard files just declare what they want, not where.
"""

from __future__ import annotations

from typing import Iterable


def grid_row(panels: Iterable[dict], y: int, *, heights: list[int] | None = None) -> tuple[list[dict], int]:
    """Lay out a list of panels in a single row, evenly splitting 24 columns.

    Returns (laid-out-panels, next_y).

    If `heights` is given, each panel gets its corresponding height; otherwise
    a default of 5 is used for all.
    """
    panels = list(panels)
    if not panels:
        return [], y
    n = len(panels)
    w = 24 // n
    remainder = 24 - (w * n)  # give leftover columns to last panel
    heights = heights or [5] * n
    assert len(heights) == n, f"heights length {len(heights)} != panel count {n}"

    max_h = max(heights)
    out = []
    x = 0
    for i, panel in enumerate(panels):
        panel_w = w + (remainder if i == n - 1 else 0)
        panel_h = heights[i]
        out.append({**panel, "gridPos": {"h": panel_h, "w": panel_w, "x": x, "y": y}})
        x += panel_w
    return out, y + max_h


def grid_stack(
    rows: list[tuple[list[dict], int]],
    *,
    start_y: int = 0,
) -> list[dict]:
    """Stack multiple rows vertically. Each row is (panels, height)."""
    out = []
    y = start_y
    for row_panels, row_height in rows:
        laid, _ = grid_row(row_panels, y, heights=[row_height] * len(row_panels))
        out.extend(laid)
        y += row_height
    return out


def assign_ids(panels: list[dict], start: int = 1) -> list[dict]:
    """Assign sequential IDs to a panel list (panels may already have gridPos)."""
    for i, p in enumerate(panels):
        p["id"] = start + i
    return panels
