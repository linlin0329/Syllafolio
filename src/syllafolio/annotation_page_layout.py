"""Partition an ordered review without changing its members or acoustic grouping."""
from __future__ import annotations

import math

from .annotation_acoustics import _require


MAX_PAGE_ITEMS = 40
LEGACY_PAGE_ITEMS = 10


def validate_page_layout(layout):
    """Return a detached, JSON-safe snapshot of the display at preparation time."""
    _require(isinstance(layout, dict) and set(layout) == {
        "mode", "count", "ms_per_pixel", "plot_width_px", "label_width_px"},
        "PAGE_LAYOUT", "Review page settings must include a mode, count, time scale and display widths.")
    mode, count, scale = layout["mode"], layout["count"], layout["ms_per_pixel"]
    _require(mode in ("auto", "fixed"), "PAGE_LAYOUT", "Choose automatic or fixed pagination.")
    _require(type(count) is int and 1 <= count <= MAX_PAGE_ITEMS, "PAGE_LAYOUT",
             f"Choose between 1 and {MAX_PAGE_ITEMS} syllables per page.")
    _require(type(scale) in (float, int) and 0.1 <= scale <= 100.0,
             "PAGE_LAYOUT", "Choose a time scale between 0.1 and 100 milliseconds per pixel.")
    _require(all(type(layout[key]) is int and 1 <= layout[key] <= 2**24
                 for key in ("plot_width_px", "label_width_px")), "PAGE_LAYOUT",
             "Review page widths must be positive logical pixel counts.")
    return dict(layout, ms_per_pixel=float(scale))


def page_capacity(layout):
    if layout is None:
        return LEGACY_PAGE_ITEMS
    if layout["mode"] == "fixed":
        return layout["count"]
    return max(1, min(MAX_PAGE_ITEMS, layout["plot_width_px"] // layout["label_width_px"]))


def page_slices(frame_counts, rate, gap_frames, layout):
    """Yield slices for one acoustic group. A long syllable is always kept whole."""
    capacity = page_capacity(layout)
    if layout is None or layout["mode"] == "fixed":
        for start in range(0, len(frame_counts), capacity):
            yield slice(start, min(start + capacity, len(frame_counts)))
        return
    # Round off sub-frame representation error (e.g. decimal 0.3 ms), never
    # include an inter-syllable gap before the first or after the final clip.
    exact_frames = layout["plot_width_px"] * layout["ms_per_pixel"] * rate / 1000
    budget = math.floor(math.nextafter(exact_frames, math.inf))
    start, used = 0, 0
    for index, frames in enumerate(frame_counts):
        if index > start and (index - start >= capacity or used + gap_frames + frames > budget):
            yield slice(start, index)
            start, used = index, 0
        if index > start:
            used += gap_frames
        used += frames
    if start < len(frame_counts):
        yield slice(start, len(frame_counts))
