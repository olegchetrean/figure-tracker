"""
Error aggregator — summarises visible robot-error observations stored on
readings (error_observed / error_type / error_severity / error_description).

Mirrors the canonicalisation approach used by analysis.get_robot_stats so the
per-robot counts use the same display names (ROSE, GARY, etc.) as the rest of
the dashboard, instead of raw OCR strings.
"""
from __future__ import annotations

from collections import defaultdict

from benchmarks import canonicalize_robot


RECENT_WINDOW_HOURS = 6.0


def _canon(name):
    if not name:
        return None
    return canonicalize_robot(name)["canonical"]


def error_summary(readings: list[dict]) -> dict:
    """
    Aggregate visible-error observations across the reading history.

    Returns:
        {
          "total_errors": int,
          "total_observations": int,
          "error_rate_percent": float,    # errors / observations * 100
          "errors_per_hour_recent": float, # over last 6h of stream
          "by_type": {"drop": 5, ...},
          "by_severity": {"low": 4, "medium": 2, "high": 1},
          "by_robot": {"ROSE": 5, "GARY": 2, ...},
          "recent_errors": [   # last 10, newest-first
            {
              "stream_hours": 110.5,
              "captured_at": "...",
              "active_robot": "ROSE",
              "type": "drop",
              "severity": "low",
              "description": "..."
            },
            ...
          ],
        }
    """
    total_observations = len(readings)

    error_rows = [r for r in readings if r.get("error_observed")]
    total_errors = len(error_rows)

    by_type: dict[str, int] = defaultdict(int)
    by_severity: dict[str, int] = defaultdict(int)
    by_robot: dict[str, int] = defaultdict(int)

    for r in error_rows:
        et = r.get("error_type") or "other"
        by_type[et] += 1

        es = r.get("error_severity") or "low"
        by_severity[es] += 1

        canon = _canon(r.get("active_robot"))
        if canon:
            by_robot[canon] += 1

    # Recent-window errors-per-hour, anchored on the latest stream_hours seen.
    errors_per_hour_recent = 0.0
    if readings:
        latest_hour = max(r["stream_hours"] for r in readings if r.get("stream_hours") is not None)
        window_start = latest_hour - RECENT_WINDOW_HOURS
        recent_count = sum(
            1 for r in error_rows
            if r.get("stream_hours") is not None and r["stream_hours"] >= window_start
        )
        errors_per_hour_recent = round(recent_count / RECENT_WINDOW_HOURS, 3)

    error_rate_percent = (
        round(total_errors / total_observations * 100, 3)
        if total_observations else 0.0
    )

    # Last 10 errors, newest-first by stream_hours.
    recent_sorted = sorted(
        error_rows,
        key=lambda r: r.get("stream_hours") or 0.0,
        reverse=True,
    )[:10]
    recent_errors = [
        {
            "stream_hours": r.get("stream_hours"),
            "captured_at":  r.get("captured_at"),
            "active_robot": _canon(r.get("active_robot")),
            "type":         r.get("error_type"),
            "severity":     r.get("error_severity"),
            "description":  r.get("error_description"),
        }
        for r in recent_sorted
    ]

    return {
        "total_errors": total_errors,
        "total_observations": total_observations,
        "error_rate_percent": error_rate_percent,
        "errors_per_hour_recent": errors_per_hour_recent,
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
        "by_severity": dict(sorted(by_severity.items(), key=lambda x: -x[1])),
        "by_robot": dict(sorted(by_robot.items(), key=lambda x: -x[1])),
        "recent_errors": recent_errors,
    }
