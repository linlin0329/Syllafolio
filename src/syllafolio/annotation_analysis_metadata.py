"""Describe the frozen acoustic inputs of a project or report."""
from __future__ import annotations

import copy
import re


def frozen_analysis(metadata, sample_rate_hz, feature_version):
    """Older Stage A manifests identify a fixed band through their recipe ID."""
    if metadata is not None:
        return copy.deepcopy(metadata)
    fixed = re.fullmatch(
        r"stage-a-(?:existing105-logmel64x32-v1|105-logmel64x32-contrast-v2)(?:-bandpass-[0-9a-f]{20})?",
        feature_version or "",
    )
    return {
        "sample_rate_hz": sample_rate_hz,
        "feature_version": feature_version,
        "mfcc_logmel_frequency_band_hz": [1000, 15000] if fixed and sample_rate_hz and sample_rate_hz >= 32000 else None,
    }


def report_analysis(frozen):
    metadata = frozen.get("acoustic_analysis")
    if metadata is not None:
        return metadata
    rates = {row.get("sample_rate_hz") for row in frozen.get("numeric_row_identity", [])}
    rate = rates.pop() if len(rates) == 1 else None
    return frozen_analysis(None, rate, frozen.get("input_versions", {}).get("feature_version"))


def analysis_rows(metadata):
    rate = metadata.get("sample_rate_hz")
    band = metadata.get("mfcc_logmel_frequency_band_hz")
    def hz(value):
        return f"{value:.5f}".rstrip("0").rstrip(".")
    return [
        ("Native sample rate", f"{rate} Hz" if rate else "Not recorded"),
        ("MFCC / log-mel frequency band", f"{hz(band[0])}–{hz(band[1])} Hz" if band else "Not recorded"),
        ("Feature recipe", metadata.get("feature_version") or "Not recorded"),
    ]
