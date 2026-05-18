"""
Builds the /api/audit payload by combining provenance + confidence + drill-downs.
Output schema matches the contract the frontend/audit.html was built against.
"""
import logging
from typing import Optional

from confidence import (
    data_quality_summary,
    confidence_for_pairwise_rate,
    confidence_for_readings,
    weight_for_source,
)
from provenance import get_recent_influence as _get_recent_influence
from analysis import _pairwise_rates, get_fixed_segments, HUMAN_BASELINE_RATE

log = logging.getLogger(__name__)

DEFAULT_INFLUENCE_LIMIT = 50
RESEARCH_SOURCES = {"research"}
HIGH_QUALITY_SOURCES = {"ocr", "ocr_manual", "screenshot"}


def _reading_summary(r: dict) -> dict:
    return {
        "reading_id": r.get("id"),
        "stream_hours": r["stream_hours"],
        "packages": r["packages"],
        "source": r.get("source", "ocr"),
    }


def _find_reading_at(readings: list, stream_hours: float, tolerance: float = 0.01) -> Optional[dict]:
    for r in readings:
        if abs(r["stream_hours"] - stream_hours) <= tolerance:
            return r
    return None


# ── Drill-downs ──────────────────────────────────────────────────────────────

def _drill_peak_low(readings: list) -> tuple[dict, dict]:
    """Identify the exact endpoints that produced the best and worst sustained rates."""
    pairs = [(readings[i - 1], readings[i], _pairwise_rates(readings[i - 1:i + 1])[0])
             for i in range(1, len(readings))
             if readings[i]["stream_hours"] > readings[i - 1]["stream_hours"]]
    pairs = [(a, b, p) for (a, b, p) in pairs if p["delta_hours"] >= 0.5]
    if not pairs:
        return {}, {}

    best = max(pairs, key=lambda x: x[2]["rate"])
    worst = min(pairs, key=lambda x: x[2]["rate"])

    def _format(a, b, p, kind):
        conf = confidence_for_pairwise_rate(a, b)
        srcs = {a.get("source", "?"), b.get("source", "?")}
        if srcs <= RESEARCH_SOURCES:
            notes = ("Both endpoints are 'research' source (sparse journalist estimates). "
                     f"{p['delta_hours']:.1f}h gap between readings — low confidence, "
                     "likely a sparse-data artefact rather than a real sustained "
                     f"{'peak' if kind == 'peak' else 'low'}.")
        elif srcs & RESEARCH_SOURCES:
            notes = f"One endpoint is 'research' (low confidence). {p['delta_hours']:.1f}h window."
        else:
            notes = f"Both endpoints are high-quality sources ({', '.join(srcs)}). {p['delta_hours']:.1f}h window."
        return {
            "value": p["rate"],
            "at_stream_hour": a["stream_hours"],
            "produced_by": [_reading_summary(a), _reading_summary(b)],
            "delta_packages": p["delta_packages"],
            "delta_hours": p["delta_hours"],
            "confidence": conf,
            "notes": notes,
        }

    return _format(*best, "peak"), _format(*worst, "low")


def _drill_learning(readings: list) -> dict:
    """Re-derive what get_learning_velocity uses and expose the underlying readings."""
    if len(readings) < 6:
        return {}
    n = len(readings)
    third = n // 3
    early = readings[:third + 1]
    recent = readings[-third - 1:]

    def chunk_rate(chunk):
        dh = chunk[-1]["stream_hours"] - chunk[0]["stream_hours"]
        dp = chunk[-1]["packages"] - chunk[0]["packages"]
        return round(dp / dh, 1) if dh > 0 else 0

    early_rate = chunk_rate(early)
    recent_rate = chunk_rate(recent)
    delta_pct = ((recent_rate - early_rate) / early_rate * 100) if early_rate else 0

    early_span = early[-1]["stream_hours"] - early[0]["stream_hours"]
    recent_span = recent[-1]["stream_hours"] - recent[0]["stream_hours"]
    early_srcs = {r.get("source", "?") for r in early}
    recent_srcs = {r.get("source", "?") for r in recent}
    conf_early = confidence_for_readings(early)
    conf_recent = confidence_for_readings(recent)

    bias_note = ""
    if early_span > 5 * recent_span and recent_span > 0:
        bias_note = (f"⚠ Window sizes differ {early_span / recent_span:.0f}× — "
                     f"early window is {early_span:.1f}h of sparse data, recent is "
                     f"{recent_span:.2f}h of dense OCR. Comparison is biased.")
    elif early_srcs <= RESEARCH_SOURCES and recent_srcs & HIGH_QUALITY_SOURCES:
        bias_note = ("⚠ Early third is entirely 'research' sources (low confidence) and "
                     "recent third is dense OCR (high confidence). Apples-to-oranges.")

    return {
        "early_rate": early_rate,
        "recent_rate": recent_rate,
        "delta_pct": round(delta_pct, 1),
        "early_readings": [_reading_summary(r) for r in early[:3] + ([early[-1]] if len(early) > 3 else [])],
        "recent_readings": [_reading_summary(r) for r in recent[:3] + ([recent[-1]] if len(recent) > 3 else [])],
        "early_span_hours": round(early_span, 3),
        "recent_span_hours": round(recent_span, 3),
        "confidence": round(min(conf_early, conf_recent), 2),
        "notes": bias_note or f"Early {early_span:.1f}h of stream → recent {recent_span:.1f}h.",
    }


# ── Suspicious entries ───────────────────────────────────────────────────────

def _flag_suspicious(readings: list) -> list[dict]:
    """Surface readings whose implied rate is wildly out of band vs neighbors."""
    flags = []
    rates = _pairwise_rates(readings)
    if len(rates) < 3:
        return flags

    median_rate = sorted([r["rate"] for r in rates])[len(rates) // 2]

    for i in range(1, len(readings)):
        prev = readings[i - 1]
        cur = readings[i]
        dh = cur["stream_hours"] - prev["stream_hours"]
        dp = cur["packages"] - prev["packages"]
        if dh <= 0:
            continue
        rate = dp / dh
        ratio = rate / median_rate if median_rate else 1
        if ratio > 2.0 or ratio < 0.4:
            severity = "high" if (ratio > 3 or ratio < 0.25) else "medium"
            flags.append({
                "reading_id": cur.get("id"),
                "stream_hours": cur["stream_hours"],
                "source": cur.get("source", "?"),
                "issue": (f"Implied rate {rate:.0f} pach/h is "
                          f"{ratio:.1f}× the median ({median_rate:.0f}/h) — "
                          f"computed from {dp:,} pkgs / {dh:.2f}h gap"),
                "severity": severity,
            })

    # Also flag rows where shift_hours is suspicious (e.g. 0 or > 9)
    for r in readings:
        sh = r.get("shift_hours")
        if sh is not None and (sh < 0 or sh > 9.1):
            flags.append({
                "reading_id": r.get("id"),
                "stream_hours": r["stream_hours"],
                "source": r.get("source", "?"),
                "issue": f"shift_hours={sh:.3f}h is outside the 0-9h work shift",
                "severity": "high",
            })
    return flags


# ── Recent influence shape transform ─────────────────────────────────────────

def _shape_recent_influence(rows: list[dict], readings_by_id: dict[int, dict],
                            limit: int = DEFAULT_INFLUENCE_LIMIT) -> list[dict]:
    """Group flat provenance rows by reading_id and shape for the frontend."""
    grouped: dict[int, dict] = {}
    for row in rows:
        rid = row.get("reading_id")
        if rid is None:
            continue
        if rid not in grouped:
            reading = readings_by_id.get(rid, {})
            grouped[rid] = {
                "reading_id": rid,
                "captured_at": row.get("computed_at"),
                "stream_hours": reading.get("stream_hours"),
                "source": reading.get("source", "ocr"),
                "changes": [],
            }
        grouped[rid]["changes"].append({
            "metric": row.get("metric_name"),
            "before": row.get("value_before"),
            "after": row.get("value_after"),
            "delta": row.get("delta"),
            "pct": row.get("pct_change"),
            "significant": bool(row.get("is_significant")),
        })
    # Most recent first
    out = sorted(grouped.values(), key=lambda x: x["reading_id"] or 0, reverse=True)
    return out[:limit]


# ── Entry point ──────────────────────────────────────────────────────────────

def build_audit_payload(readings: list[dict]) -> dict:
    """Compose the full /api/audit response."""
    if not readings:
        return {
            "data_quality": {
                "overall_confidence": 0.0,
                "total_readings": 0,
                "by_source": [],
                "coverage_gaps": [],
                "warnings": ["No readings yet"],
            },
            "recent_influence": [],
            "drill_downs": {},
            "suspicious_entries": [],
        }

    dq = data_quality_summary(readings)
    peak, low = _drill_peak_low(readings)
    learning = _drill_learning(readings)

    readings_by_id = {r["id"]: r for r in readings if r.get("id")}
    try:
        prov_rows = _get_recent_influence(limit=DEFAULT_INFLUENCE_LIMIT * 8)
    except Exception as exc:
        log.warning("provenance read failed: %s", exc)
        prov_rows = []

    return {
        "data_quality": dq,
        "recent_influence": _shape_recent_influence(prov_rows, readings_by_id),
        "drill_downs": {
            "peak_rate": peak,
            "low_rate": low,
            "learning_delta": learning,
        },
        "suspicious_entries": _flag_suspicious(readings),
    }
