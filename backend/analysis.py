from typing import Optional
import numpy as np

from benchmarks import (
    rate_by_phase,
    rate_distribution,
    time_of_day_pattern,
    streaks,
    predictive_etas,
    shift_records,
    normalize_robot_identities,
    canonicalize_robot,
    rate_pulse,
    package_mix_summary,
    get_achievements,
)
from error_aggregator import error_summary
from narrative import master_conclusion, segment_narratives, error_narratives


SEGMENT_WINDOWS = [0.5, 1, 3, 6, 12, 24]   # hours for rolling rate windows
HUMAN_BASELINE_RATE = 1431.0                # pach/h research baseline
DOCK_RATE_THRESHOLD = 400                   # pach/h below = probable downtime
CUMULATIVE_CURVE_POINTS = 240               # max points sent to the chart
SHIFT_TOTAL_HOURS = 9.0                     # The "FULL WORK SHIFT" is 9h (shift_hours stored as elapsed)


# ── Core helpers ─────────────────────────────────────────────────────────────

def _pairwise_rates(readings: list) -> list:
    """Packages/hour for every consecutive pair of readings."""
    rates = []
    for i in range(1, len(readings)):
        dh = readings[i]["stream_hours"] - readings[i - 1]["stream_hours"]
        dp = readings[i]["packages"] - readings[i - 1]["packages"]
        if dh > 0 and dp >= 0:
            rates.append(
                {
                    "hour": readings[i - 1]["stream_hours"],
                    "hour_end": readings[i]["stream_hours"],
                    "rate": round(dp / dh, 1),
                    "delta_packages": dp,
                    "delta_hours": round(dh, 4),
                }
            )
    return rates


def get_rate_for_window(readings: list, window_hours: float) -> Optional[float]:
    """Packages/hour over the last `window_hours` of stream time."""
    if len(readings) < 2:
        return None
    latest = readings[-1]["stream_hours"]
    cutoff = latest - window_hours
    window = [r for r in readings if r["stream_hours"] >= cutoff]
    if len(window) < 2:
        return None
    dh = window[-1]["stream_hours"] - window[0]["stream_hours"]
    dp = window[-1]["packages"] - window[0]["packages"]
    return round(dp / dh, 1) if dh > 0 else None


def get_fixed_segments(readings: list, window_hours: float, anchor: Optional[float] = None) -> list:
    """
    Slice the stream timeline into fixed chunks of `window_hours`.
    `anchor` lets segments align to a specific clock (e.g., 0 for day boundaries
    instead of stream start).
    """
    if len(readings) < 2:
        return []
    start = readings[0]["stream_hours"]
    latest = readings[-1]["stream_hours"]
    segments = []

    if anchor is not None:
        cursor = anchor + np.floor((start - anchor) / window_hours) * window_hours
    else:
        cursor = start

    while cursor < latest:
        end = cursor + window_hours
        chunk = [r for r in readings if cursor <= r["stream_hours"] <= end]
        if len(chunk) >= 2:
            dh = chunk[-1]["stream_hours"] - chunk[0]["stream_hours"]
            dp = chunk[-1]["packages"] - chunk[0]["packages"]
            if dh > 0:
                segments.append(
                    {
                        "start_hour": round(cursor, 3),
                        "end_hour": round(min(end, latest), 3),
                        "packages_per_hour": round(dp / dh, 1),
                        "packages_count": dp,
                        "coverage_hours": round(dh, 3),
                    }
                )
        cursor = end
    return segments


# ── Trend & learning ─────────────────────────────────────────────────────────

def get_trend(readings: list) -> tuple:
    """Linear regression slope of packages/hour over stream time."""
    rates = _pairwise_rates(readings)
    if len(rates) < 3:
        return 0.0, "insufficient_data"

    x = np.array([r["hour"] for r in rates], dtype=float)
    y = np.array([r["rate"] for r in rates], dtype=float)
    coeffs = np.polyfit(x, y, 1)
    slope = float(coeffs[0])

    if slope > 5:
        status = "improving"
    elif slope < -5:
        status = "declining"
    else:
        status = "stable"
    return round(slope, 4), status


def get_learning_velocity(readings: list) -> dict:
    """
    Time-equal comparison: rate over the last `window_hours` of stream time vs
    the rate over an equal `window_hours` slice from EARLY in the stream.

    Avoids the previous bug where count-equal thirds compared 90h of sparse
    journalist data against 0.5h of dense OCR (168× span mismatch).
    """
    if len(readings) < 6:
        return {"available": False, "reason": "need ≥ 6 readings"}

    latest = readings[-1]
    earliest = readings[0]
    total_span = latest["stream_hours"] - earliest["stream_hours"]
    if total_span < 4:
        return {"available": False, "reason": f"total span {total_span:.1f}h < 4h"}

    # Use up to last 6h for "recent"; mirror window for "early"
    window_hours = min(6.0, total_span / 3)
    recent_cutoff = latest["stream_hours"] - window_hours
    early_cutoff  = earliest["stream_hours"] + window_hours

    recent_rows = [r for r in readings if r["stream_hours"] >= recent_cutoff]
    early_rows  = [r for r in readings if r["stream_hours"] <= early_cutoff]
    if len(recent_rows) < 2 or len(early_rows) < 2:
        return {"available": False, "reason": "insufficient density in one window"}

    def _rate(chunk):
        dh = chunk[-1]["stream_hours"] - chunk[0]["stream_hours"]
        dp = chunk[-1]["packages"] - chunk[0]["packages"]
        return round(dp / dh, 1) if dh > 0 else 0

    early = _rate(early_rows)
    recent = _rate(recent_rows)
    delta = recent - early
    delta_pct = (delta / early * 100) if early > 0 else 0

    return {
        "available": True,
        "window_hours": round(window_hours, 2),
        "early_rate": early,
        "recent_rate": recent,
        "delta_rate": round(delta, 1),
        "delta_percent": round(delta_pct, 1),
        "improving": delta > 0,
        "early_window": f"first {window_hours:.1f}h of stream "
                        f"({early_rows[0]['stream_hours']:.1f}→{early_rows[-1]['stream_hours']:.1f}h, "
                        f"{len(early_rows)} reads, sources={_distinct_sources(early_rows)})",
        "recent_window": f"last {window_hours:.1f}h of stream "
                         f"({recent_rows[0]['stream_hours']:.1f}→{recent_rows[-1]['stream_hours']:.1f}h, "
                         f"{len(recent_rows)} reads, sources={_distinct_sources(recent_rows)})",
    }


def _distinct_sources(rows: list) -> str:
    seen = []
    for r in rows:
        s = r.get("source", "?")
        if s not in seen:
            seen.append(s)
    return "/".join(seen)


# ── Peaks & insights ─────────────────────────────────────────────────────────

def get_peak_rate(readings: list) -> dict:
    """
    Best and worst sustained packages/hour anywhere in the stream.
    Considers all pairwise segments spanning at least 30 minutes — covers both
    sparse historical chunks and dense OCR hourly windows.
    """
    rates = [r for r in _pairwise_rates(readings) if r["delta_hours"] >= 0.5]
    if not rates:
        return {"available": False}
    best = max(rates, key=lambda r: r["rate"])
    worst = min(rates, key=lambda r: r["rate"])
    return {
        "available": True,
        "peak_rate": best["rate"],
        "peak_hour": best["hour"],
        "peak_span": round(best["delta_hours"], 2),
        "low_rate": worst["rate"],
        "low_hour": worst["hour"],
        "low_span": round(worst["delta_hours"], 2),
    }


def get_dock_events(readings: list) -> list:
    """Probable charging dock events — low rate over a short span."""
    rates = _pairwise_rates(readings)
    events = []
    for r in rates:
        if r["rate"] < DOCK_RATE_THRESHOLD and r["delta_hours"] < 1.5:
            events.append(
                {
                    "hour": r["hour"],
                    "duration_h": r["delta_hours"],
                    "rate_during": r["rate"],
                }
            )
    return events


def predict_agi_moment(readings: list) -> dict:
    """
    Robot vs human comparison.
    `active` only when a split-screen reading was captured RECENTLY
    (within the last 30 min of stream time). Otherwise we still expose the
    LAST KNOWN result of the most recent challenge as a historical record.
    """
    split_readings = [
        r for r in readings
        if r.get("is_split") and r.get("human_packages") and r.get("robot_packages")
        and r.get("shift_hours") and r["shift_hours"] > 0
    ]

    if split_readings and readings:
        latest_split   = split_readings[-1]
        latest_overall = readings[-1]
        # Stream-time distance between latest split and "now"
        time_since_split = latest_overall["stream_hours"] - latest_split["stream_hours"]
        is_recent = time_since_split <= 0.5   # 30 min of stream time

        avg_human = latest_split["human_packages"] / latest_split["shift_hours"]
        avg_robot = latest_split["robot_packages"] / latest_split["shift_hours"]
        gap_pct = ((avg_human - avg_robot) / avg_human) * 100 if avg_human > 0 else 0
        return {
            "active": is_recent,
            "challenge_ended_hours_ago": round(time_since_split, 2) if not is_recent else 0,
            "human_rate": round(avg_human, 1),
            "robot_rate": round(avg_robot, 1),
            "human_packages": latest_split["human_packages"],
            "robot_packages": latest_split["robot_packages"],
            "gap_percent": round(gap_pct, 1),
            "robot_winning": avg_robot >= avg_human,
            "shift_hours": round(latest_split["shift_hours"], 2),
            "last_split_at_stream_hour": round(latest_split["stream_hours"], 2),
        }

    current_robot_rate = get_rate_for_window(readings, 1) or 1252.0
    gap_pct = ((HUMAN_BASELINE_RATE - current_robot_rate) / HUMAN_BASELINE_RATE) * 100
    return {
        "active": False,
        "human_rate": HUMAN_BASELINE_RATE,
        "robot_rate": round(current_robot_rate, 1),
        "gap_percent": round(gap_pct, 1),
        "robot_winning": current_robot_rate >= HUMAN_BASELINE_RATE,
        "shift_hours": None,
    }


# ── Cumulative curve ─────────────────────────────────────────────────────────

def get_cumulative_curve(readings: list) -> list:
    """
    Cumulative packages over stream_hours, downsampled if needed.
    Keeps the first + last point and evenly spreads the rest.
    """
    if not readings:
        return []
    if len(readings) <= CUMULATIVE_CURVE_POINTS:
        return [{"x": r["stream_hours"], "y": r["packages"]} for r in readings]

    idx = np.linspace(0, len(readings) - 1, CUMULATIVE_CURVE_POINTS, dtype=int)
    return [
        {"x": readings[i]["stream_hours"], "y": readings[i]["packages"]}
        for i in idx
    ]


# ── Per-robot analysis ───────────────────────────────────────────────────────

def get_robot_stats(readings: list) -> dict:
    """
    Accumulate per-robot package totals + hours, attributing each consecutive
    delta to the robot named on the LATER reading (only when the previous reading
    named the same robot — otherwise it's a handoff and the work is split).

    Names are canonicalised against the known robot whitelist so OCR typos
    (e.g. "BOSE" → "ROSE") don't create phantom robots in the leaderboard.
    """
    def _canon(r):
        raw = r.get("active_robot") if r else None
        return canonicalize_robot(raw)["canonical"] if raw else None

    stats: dict = {}
    last = None
    last_canon = None
    for r in readings:
        rb = _canon(r)
        if rb:
            stats.setdefault(rb, {
                "name": rb,
                "model": r.get("robot_model"),
                "packages_attributed": 0,
                "hours_worked": 0.0,
                "first_seen_at": r["stream_hours"],
                "last_seen_at":  r["stream_hours"],
                "appearances":   0,
                "handoff_count": 0,
            })
            stats[rb]["last_seen_at"] = r["stream_hours"]
            stats[rb]["appearances"] += 1
        if last and rb and last_canon == rb:
            dh = r["stream_hours"] - last["stream_hours"]
            dp = r["packages"] - last["packages"]
            if dh > 0 and dp >= 0:
                stats[rb]["packages_attributed"] += dp
                stats[rb]["hours_worked"] += dh
        elif last and rb and last_canon and last_canon != rb:
            stats[rb]["handoff_count"] += 1
        last = r
        last_canon = rb

    for s in stats.values():
        if s["hours_worked"] > 0:
            s["rate"] = round(s["packages_attributed"] / s["hours_worked"], 1)
        else:
            s["rate"] = None
        s["hours_worked"] = round(s["hours_worked"], 3)
    return stats


def get_robot_shifts(readings: list) -> list:
    """
    Timeline of CONTINUOUS-RUN shifts per robot — every time the active_robot
    name changes (or transitions in/out of None) starts a new shift entry.

    Differs from `shift_records` (which counts 9h split-challenge episodes):
    these are operational solo-run shifts a particular robot worked.
    """
    def _canon(r):
        raw = r.get("active_robot") if r else None
        return canonicalize_robot(raw)["canonical"] if raw else None

    shifts: list[dict] = []
    cur = None
    for r in readings:
        rb = _canon(r)
        if rb is None:
            if cur is not None:
                shifts.append(cur)
                cur = None
            continue
        if cur and cur["robot"] == rb:
            cur["end_hour"] = r["stream_hours"]
            cur["end_packages"] = r["packages"]
            cur["last_captured_at"] = r.get("captured_at")
            cur["readings"] += 1
        else:
            if cur is not None:
                shifts.append(cur)
            cur = {
                "robot": rb,
                "model": r.get("robot_model"),
                "start_hour": r["stream_hours"],
                "end_hour":   r["stream_hours"],
                "start_packages": r["packages"],
                "end_packages":   r["packages"],
                "first_captured_at": r.get("captured_at"),
                "last_captured_at":  r.get("captured_at"),
                "readings": 1,
            }
    if cur is not None:
        shifts.append(cur)

    for s in shifts:
        s["duration_hours"]    = round(s["end_hour"] - s["start_hour"], 3)
        s["packages_in_shift"] = s["end_packages"] - s["start_packages"]
        s["rate"] = (round(s["packages_in_shift"] / s["duration_hours"], 1)
                     if s["duration_hours"] > 0 else None)
    return shifts


def get_handoff_events(readings: list, limit: int = 20) -> list:
    """
    List the moments where active_robot changed between consecutive readings.
    Names are canonicalised so OCR typos don't create false handoffs.
    """
    def _canon(r):
        raw = r.get("active_robot") if r else None
        return canonicalize_robot(raw)["canonical"] if raw else None

    events = []
    last = None
    last_canon = None
    for r in readings:
        rb = _canon(r)
        if last and last_canon and rb and last_canon != rb:
            events.append({
                "at_stream_hour": r["stream_hours"],
                "captured_at": r.get("captured_at"),
                "from_robot": last_canon,
                "to_robot":   rb,
                "packages_at_handoff": r["packages"],
            })
        last = r
        last_canon = rb
    return events[-limit:]


def get_phase_timeline(readings: list) -> list:
    """Compress consecutive same-phase readings into a timeline of phase segments."""
    timeline = []
    cur = None
    for r in readings:
        ph = r.get("phase") or ("split_challenge" if r.get("is_split") else "unknown")
        if cur and cur["phase"] == ph:
            cur["end_hour"] = r["stream_hours"]
            cur["end_packages"] = r["packages"]
            cur["readings"] += 1
        else:
            if cur:
                timeline.append(cur)
            cur = {
                "phase": ph,
                "start_hour": r["stream_hours"],
                "end_hour":   r["stream_hours"],
                "start_packages": r["packages"],
                "end_packages":   r["packages"],
                "readings": 1,
            }
    if cur:
        timeline.append(cur)
    for seg in timeline:
        seg["duration_hours"] = round(seg["end_hour"] - seg["start_hour"], 3)
        seg["packages_in_segment"] = seg["end_packages"] - seg["start_packages"]
        seg["rate"] = (round(seg["packages_in_segment"] / seg["duration_hours"], 1)
                       if seg["duration_hours"] > 0 else None)
    return timeline


def get_current_status(readings: list) -> dict:
    """Snapshot of who/what is happening right now (from the latest reading with identity)."""
    if not readings:
        return {"available": False}
    latest = readings[-1]
    # Find most recent reading that has an active_robot (canonical)
    for r in reversed(readings):
        canon = canonicalize_robot(r.get("active_robot"))["canonical"]
        if canon:
            # How long has this canonical robot been continuously working?
            run_start = r["stream_hours"]
            run_packages_start = r["packages"]
            for prev in reversed(readings):
                if prev["stream_hours"] < r["stream_hours"]:
                    prev_canon = canonicalize_robot(prev.get("active_robot"))["canonical"]
                    if prev_canon == canon:
                        run_start = prev["stream_hours"]
                        run_packages_start = prev["packages"]
                    else:
                        break
            return {
                "available": True,
                "active_robot": canon,
                "robot_model":  r.get("robot_model"),
                "phase":        r.get("phase") or "unknown",
                "stream_hours": latest["stream_hours"],
                "packages":     latest["packages"],
                "run_started_at_hour": run_start,
                "run_duration_hours":  round(latest["stream_hours"] - run_start, 3),
                "run_packages":        latest["packages"] - run_packages_start,
                "run_rate": (round((latest["packages"] - run_packages_start) /
                                   (latest["stream_hours"] - run_start), 1)
                             if latest["stream_hours"] > run_start else None),
            }
    return {
        "available": False,
        "phase": latest.get("phase") or "unknown",
        "stream_hours": latest["stream_hours"],
        "packages": latest["packages"],
        "reason": "No reading yet carries an active_robot identity — capture in progress.",
    }


# ── Learning verdict ─────────────────────────────────────────────────────────

def get_verdict(readings: list, learning: dict, agi: dict, peak: dict, daily_segs: list) -> dict:
    """
    Synthesize all metrics into a single, transparent answer:
        Is the F.03 robot getting better?
    Returns a verdict with a numeric score, confidence, and an evidence list
    where every claim is paired with the exact numbers behind it.
    """
    if len(readings) < 6:
        return {
            "available": False,
            "verdict": "insufficient_data",
            "title": "Not enough data yet",
            "summary": f"Only {len(readings)} readings collected. Need at least 6 for a verdict.",
            "score": 0,
            "confidence": 0.0,
            "evidence": [],
        }

    evidence = []
    score = 0

    # 1. Recent vs early throughput
    if learning.get("available"):
        dp = learning["delta_percent"]
        sign = "+" if dp >= 0 else ""
        if dp > 10:
            verdict, weight = "positive", 3
        elif dp > 5:
            verdict, weight = "positive", 2
        elif dp >= -5:
            verdict, weight = "neutral", 0
        elif dp >= -10:
            verdict, weight = "negative", -2
        else:
            verdict, weight = "negative", -3
        score += weight
        evidence.append({
            "label":  "Recent vs early throughput",
            "detail": "Avg rate of last third of stream vs first third",
            "before": f"{learning['early_rate']:,.0f} pach/h",
            "after":  f"{learning['recent_rate']:,.0f} pach/h",
            "delta":  f"{sign}{dp:.1f}%",
            "verdict": verdict,
            "weight": abs(weight),
        })

    # 2. Robot vs research human baseline (always meaningful)
    overall = readings[-1]["packages"] / readings[-1]["stream_hours"] if readings[-1]["stream_hours"] else 0
    recent_1h = get_rate_for_window(readings, 1) or overall
    diff_pct = (recent_1h - HUMAN_BASELINE_RATE) / HUMAN_BASELINE_RATE * 100
    if diff_pct > 5:
        verdict, weight = "positive", 2
    elif diff_pct >= -5:
        verdict, weight = "neutral", 0
    else:
        verdict, weight = "negative", -2
    score += weight
    evidence.append({
        "label":  "Robot vs research human baseline",
        "detail": "Robot's most-recent 1h rate vs 1,431 pach/h research benchmark",
        "before": f"{HUMAN_BASELINE_RATE:,.0f} pach/h (baseline)",
        "after":  f"{recent_1h:,.0f} pach/h",
        "delta":  f"{'+' if diff_pct >= 0 else ''}{diff_pct:.1f}%",
        "verdict": verdict,
        "weight": abs(weight),
    })

    # 3. Robot vs live human in current challenge
    if agi.get("active"):
        gap = agi["gap_percent"]
        if agi["robot_winning"]:
            verdict, weight, delta_str = "positive", 3, "WINNING"
        elif gap < 3:
            verdict, weight, delta_str = "positive", 2, f"{gap:.1f}% behind"
        elif gap < 10:
            verdict, weight, delta_str = "neutral", 1, f"{gap:.1f}% behind"
        elif gap < 25:
            verdict, weight, delta_str = "neutral", 0, f"{gap:.1f}% behind"
        else:
            verdict, weight, delta_str = "negative", -2, f"{gap:.1f}% behind"
        score += weight
        evidence.append({
            "label":  "Robot vs human in live challenge",
            "detail": f"Cumulative rates over {agi['shift_hours']:.2f}h elapsed of the 9h work shift",
            "before": f"Human {agi['human_rate']:,.0f} pach/h ({agi.get('human_packages', 0):,} pkgs)",
            "after":  f"Robot {agi['robot_rate']:,.0f} pach/h ({agi.get('robot_packages', 0):,} pkgs)",
            "delta":  delta_str,
            "verdict": verdict,
            "weight": abs(weight) if weight else 1,
        })

    # 4. Day-over-day comparison (if we have at least 2 complete days)
    if daily_segs and len(daily_segs) >= 2:
        first = daily_segs[0]["packages_per_hour"]
        last  = daily_segs[-1]["packages_per_hour"]
        delta = ((last - first) / first * 100) if first else 0
        sign  = "+" if delta >= 0 else ""
        if delta > 5:
            verdict, weight = "positive", 1
        elif delta >= -5:
            verdict, weight = "neutral", 0
        else:
            verdict, weight = "negative", -1
        score += weight
        evidence.append({
            "label":  f"Day 1 vs Day {len(daily_segs)} (24h windows)",
            "detail": "Average rate across the first and last 24-hour chunks of stream time",
            "before": f"{first:,.0f} pach/h",
            "after":  f"{last:,.0f} pach/h",
            "delta":  f"{sign}{delta:.1f}%",
            "verdict": verdict,
            "weight": abs(weight) if weight else 1,
        })

    # 5. Peak performance reference (always shown, no scoring impact)
    if peak.get("available"):
        evidence.append({
            "label":  "Best sustained hour ever",
            "detail": f"Highest rate sustained for >= 30 min",
            "before": "—",
            "after":  f"{peak['peak_rate']:,.0f} pach/h at stream hour {peak['peak_hour']:.1f}",
            "delta":  f"{peak['peak_span']:.1f}h window",
            "verdict": "neutral",
            "weight": 1,
        })

    # Confidence scales with data volume (cap at 60 readings)
    confidence = round(min(1.0, len(readings) / 60.0), 2)

    # Translate cumulative score into a clear verdict
    if score >= 5:
        verdict_key = "strong_improvement"
        title = "YES — robot is clearly improving"
        klass = "positive"
    elif score >= 2:
        verdict_key = "moderate_improvement"
        title = "YES — robot is improving"
        klass = "positive"
    elif score >= -1:
        verdict_key = "stagnating"
        title = "INCONCLUSIVE — performance is stable, no clear trend"
        klass = "neutral"
    else:
        verdict_key = "declining"
        title = "NO — robot is getting worse"
        klass = "negative"

    # Build a 1–2 sentence summary using the positive evidence
    positives = [e for e in evidence if e["verdict"] == "positive"]
    if positives:
        bits = [f"{e['label'].lower()}: {e['after']} ({e['delta']})" for e in positives[:3]]
        summary = "; ".join(bits) + "."
        summary = summary[0].upper() + summary[1:]
    else:
        summary = "No metric crossed the threshold for a positive verdict."

    return {
        "available": True,
        "verdict": verdict_key,
        "verdict_class": klass,
        "title": title,
        "summary": summary,
        "score": score,
        "confidence": confidence,
        "data_points": len(readings),
        "evidence": evidence,
    }


# ── Main analysis ────────────────────────────────────────────────────────────

def full_analysis(readings: list) -> dict:
    if not readings:
        return {"error": "no_data"}

    latest = readings[-1]
    total_hours = latest["stream_hours"]
    total_packages = latest["packages"]
    overall_rate = round(total_packages / total_hours, 1) if total_hours > 0 else 0

    slope, trend = get_trend(readings)
    all_rates = _pairwise_rates(readings)

    rates_by_window = {}
    for w in SEGMENT_WINDOWS:
        label = f"{w}h" if w >= 1 else "30m"
        rates_by_window[label] = get_rate_for_window(readings, w)

    # Hourly segments: historical pairwise rates (>= 30 min span) + recent 1h windows
    HISTORICAL_MIN_HOURS = 0.5
    historical_segs = [
        {
            "start_hour": r["hour"],
            "end_hour": r["hour_end"],
            "packages_per_hour": r["rate"],
            "packages_count": r["delta_packages"],
            "coverage_hours": r["delta_hours"],
        }
        for r in all_rates
        if r["delta_hours"] >= HISTORICAL_MIN_HOURS
    ]

    recent_cutoff = total_hours - 6
    recent_reads = [r for r in readings if r["stream_hours"] >= recent_cutoff]
    recent_segs = get_fixed_segments(recent_reads, 1)

    recent_start = recent_segs[0]["start_hour"] if recent_segs else total_hours
    hourly_segs = [s for s in historical_segs if s["end_hour"] <= recent_start] + recent_segs

    # Daily breakdown — 24h chunks anchored at stream start
    daily_segs = get_fixed_segments(readings, 24)

    peak       = get_peak_rate(readings)
    learning   = get_learning_velocity(readings)
    agi        = predict_agi_moment(readings)
    verdict    = get_verdict(readings, learning, agi, peak, daily_segs)
    robots     = get_robot_stats(readings)
    handoffs   = get_handoff_events(readings)
    phases     = get_phase_timeline(readings)
    current    = get_current_status(readings)
    robot_shfts = get_robot_shifts(readings)
    errors     = error_summary(readings)

    # Advanced benchmarks (from benchmarks.py)
    phase_rates  = rate_by_phase(readings)
    distribution = rate_distribution(readings)
    tod_pattern  = time_of_day_pattern(readings)
    streak_data  = streaks(readings)
    current_rate_for_eta = (rates_by_window.get("1h") or
                            rates_by_window.get("3h") or
                            overall_rate)
    etas         = predictive_etas(readings, current_rate_for_eta)
    shifts       = shift_records(readings)
    identity     = normalize_robot_identities(readings)
    pulse        = rate_pulse(readings)
    package_mix  = package_mix_summary(readings)

    result = {
        "latest": latest,
        "total_packages": total_packages,
        "total_hours": round(total_hours, 3),
        "overall_rate": overall_rate,
        "trend": trend,
        "trend_slope": slope,
        "rates_by_window": rates_by_window,
        "hourly_segments": hourly_segs,
        "six_hour_segments": get_fixed_segments(readings, 6),
        "daily_segments": daily_segs,
        "all_rates": all_rates,
        "cumulative_curve": get_cumulative_curve(readings),
        "dock_events": get_dock_events(readings),
        "peak": peak,
        "learning": learning,
        "agi": agi,
        "verdict": verdict,
        "robots": robots,
        "robot_shifts": robot_shfts,
        "handoffs": handoffs,
        "phase_timeline": phases,
        "current_status": current,
        "benchmarks": {
            "rate_by_phase": phase_rates,
            "rate_distribution": distribution,
            "time_of_day_pattern": tod_pattern,
            "streaks": streak_data,
            "predictive_etas": etas,
            "shift_records": shifts,
            "robot_identities": identity,
            "rate_pulse": pulse,
            "package_mix": package_mix,
        },
        "errors": errors,
        "reading_count": len(readings),
    }
    # Narrative depends on the full analysis dict, so we build it last
    result["narrative"] = {
        "master": master_conclusion(result),
        "segments": segment_narratives(readings, result),
        "errors": error_narratives(errors.get("recent_errors", [])),
    }
    # Achievements need the full result for things like peak/shifts
    result["achievements"] = get_achievements(readings, result)
    return result
