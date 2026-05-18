"""
Confidence / weighting module for the Figure AI tracking system.

Each reading carries a `source` field with a different trust level. This module
scores how trustworthy a derived metric is given which sources contributed
and how densely they sampled the timeline.
"""

from typing import Optional


SOURCE_WEIGHTS = {
    "research":   0.50,
    "screenshot": 0.85,
    "ocr_manual": 0.90,
    "ocr":        1.00,
}

SOURCE_DESCRIPTIONS = {
    "research":   "Sparse estimates from news articles and tweets — rate is interpolated between widely-spaced points",
    "screenshot": "User-provided screenshot manually transcribed — exact values but only a single moment in time",
    "ocr_manual": "Manual OCR from inspecting a frame — verified visually",
    "ocr":        "Live vision OCR running every 2 minutes via gpt-5.4-nano — densely sampled",
}

MIN_READINGS_FOR_FULL_CONFIDENCE = 30

_UNKNOWN_SOURCE_WEIGHT = 0.6
_GAP_THRESHOLD_HOURS = 5.0


# ── Public API ───────────────────────────────────────────────────────────────

def weight_for_source(source: str) -> float:
    """Per-source trust weight, with a conservative default for unknown sources."""
    return SOURCE_WEIGHTS.get(source, _UNKNOWN_SOURCE_WEIGHT)


def describe_source(source: str) -> str:
    """Human-readable description of a source."""
    return SOURCE_DESCRIPTIONS.get(source, f"Unknown source '{source}'")


def confidence_for_readings(readings: list) -> float:
    """
    Combined confidence (0-1) for a list of readings.

      source_weight = mean of per-reading source weights
      volume_weight = min(1, n / MIN_READINGS_FOR_FULL_CONFIDENCE)
      return        = source_weight * volume_weight   (rounded to 2 dp)
    """
    if not readings:
        return 0.0
    weights = [weight_for_source(r.get("source", "")) for r in readings]
    source_weight = sum(weights) / len(weights)
    volume_weight = min(1.0, len(readings) / MIN_READINGS_FOR_FULL_CONFIDENCE)
    return round(source_weight * volume_weight, 2)


def confidence_for_pairwise_rate(reading_a: dict, reading_b: dict) -> float:
    """
    Confidence of a rate derived from two endpoints (e.g. peak_rate, daily_rate).

    Geometric mean of the two source weights, multiplied by a density factor
    that penalises wide spans between the endpoints.
    """
    if not reading_a or not reading_b:
        return 0.0

    wa = weight_for_source(reading_a.get("source", ""))
    wb = weight_for_source(reading_b.get("source", ""))
    geo_mean = (wa * wb) ** 0.5

    ha = reading_a.get("stream_hours")
    hb = reading_b.get("stream_hours")
    if ha is None or hb is None:
        density = 0.4
    else:
        delta_hours = abs(hb - ha)
        if delta_hours <= 2:
            density = 1.0
        elif delta_hours <= 6:
            density = 0.7
        else:
            density = 0.4

    return round(geo_mean * density, 2)


def confidence_for_window(readings: list, window_hours: float, end_hour: float) -> float:
    """
    Confidence for "the rate over the last `window_hours` of stream time",
    ending at `end_hour`.

      - Filter readings to [end_hour - window_hours, end_hour].
      - Base confidence comes from confidence_for_readings(window).
      - Penalty: if fewer than 3 readings in window, multiply by len/3.
      - Penalty: if the window extends beyond available data, multiply by
        the coverage ratio (actual span ÷ requested window).
    """
    if not readings or window_hours <= 0:
        return 0.0

    cutoff = end_hour - window_hours
    window = [
        r for r in readings
        if r.get("stream_hours") is not None
        and cutoff <= r["stream_hours"] <= end_hour
    ]
    if not window:
        return 0.0

    base = confidence_for_readings(window)

    # Sparse-window penalty
    if len(window) < 3:
        base *= len(window) / 3.0

    # Coverage penalty — how much of the requested window is actually covered
    hours = [r["stream_hours"] for r in window]
    span = max(hours) - min(hours)
    if span <= 0:
        # All readings at one instant: cap coverage low
        coverage = 1.0 / window_hours if window_hours > 0 else 0.0
        coverage = min(1.0, coverage)
    else:
        coverage = min(1.0, span / window_hours)
    base *= coverage

    return round(base, 2)


def data_quality_summary(all_readings: list) -> dict:
    """
    A self-contained snapshot of how much we trust the dataset overall.
    Safe to call with an empty list.
    """
    if not all_readings:
        return {
            "overall_confidence": 0.0,
            "total_readings": 0,
            "by_source": [],
            "coverage_gaps": [],
            "warnings": ["No readings available."],
        }

    total = len(all_readings)
    overall = confidence_for_readings(all_readings)

    # ── Per-source breakdown ──
    by_source_map: dict = {}
    for r in all_readings:
        src = r.get("source", "unknown")
        by_source_map.setdefault(src, []).append(r)

    by_source = []
    for src, items in by_source_map.items():
        hours = [
            i["stream_hours"] for i in items
            if i.get("stream_hours") is not None
        ]
        first_hour = round(min(hours), 2) if hours else None
        last_hour = round(max(hours), 2) if hours else None
        hours_covered = round(last_hour - first_hour, 2) if hours else 0.0
        by_source.append({
            "source": src,
            "count": len(items),
            "hours_covered": hours_covered,
            "weight": weight_for_source(src),
            "first_hour": first_hour,
            "last_hour": last_hour,
            "description": describe_source(src),
        })
    # Stable, readable order: highest weight first, then by count desc
    by_source.sort(key=lambda s: (-s["weight"], -s["count"]))

    # ── Coverage gaps ──
    sorted_readings = sorted(
        [r for r in all_readings if r.get("stream_hours") is not None],
        key=lambda r: r["stream_hours"],
    )
    coverage_gaps = []
    for i in range(1, len(sorted_readings)):
        a = sorted_readings[i - 1]["stream_hours"]
        b = sorted_readings[i]["stream_hours"]
        if b - a > _GAP_THRESHOLD_HOURS:
            coverage_gaps.append((round(a, 2), round(b, 2)))

    # ── Warnings ──
    warnings: list = []
    research_count = sum(
        1 for r in all_readings if r.get("source") == "research"
    )
    if research_count:
        warnings.append(
            f"{research_count} of {total} readings are 'research' (low confidence)"
        )

    if coverage_gaps:
        largest = max(coverage_gaps, key=lambda g: g[1] - g[0])
        gap_size = round(largest[1] - largest[0], 2)
        warnings.append(
            f"Largest data gap: {gap_size} hours ({largest[0]}h → {largest[1]}h)"
        )

    if total < MIN_READINGS_FOR_FULL_CONFIDENCE:
        warnings.append(
            f"Only {total} readings — need {MIN_READINGS_FOR_FULL_CONFIDENCE} for full volume confidence"
        )

    return {
        "overall_confidence": overall,
        "total_readings": total,
        "by_source": by_source,
        "coverage_gaps": coverage_gaps,
        "warnings": warnings,
    }


def confidence_label(c: float) -> str:
    """Bucket a 0-1 confidence into a human label."""
    if c is None:
        return "VERY LOW"
    if c >= 0.85:
        return "HIGH"
    if c >= 0.65:
        return "MEDIUM"
    if c >= 0.40:
        return "LOW"
    return "VERY LOW"
