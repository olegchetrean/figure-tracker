"""
Advanced benchmark calculations — parameters identified in INVESTIGATION.md
that go beyond simple rate-per-window:

  - rate_by_phase           — separate rate for solo_active / split_challenge / unknown
  - rate_distribution       — mean, stddev, percentiles, CV (consistency)
  - time_of_day_pattern     — rate aggregated by UTC hour of capture
  - streaks                 — longest run above/below median rate
  - predictive_etas         — when reaches package milestones
  - shift_records           — completed split_challenge outcomes
  - identity_canonicalization — robust active_robot name (handles OCR typos)

All functions are pure: take a list of reading dicts, return JSON-safe dicts/lists.
None of them mutate input.
"""
from __future__ import annotations
import logging
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Iterable

log = logging.getLogger(__name__)

PHASE_LABELS = ("split_challenge", "solo_active", "unknown", "transition", "ambient")
MILESTONES = (150_000, 175_000, 200_000, 250_000)
ROBOT_WHITELIST = ("ROSE", "GARY", "JIM", "BOB", "ALICE", "FRANK", "F.03")


# ── small numeric helpers ────────────────────────────────────────────────────

def _percentiles(values: list[float], pcts: Iterable[float]) -> dict:
    if not values:
        return {f"p{int(p*100)}": None for p in pcts}
    sv = sorted(values)
    out = {}
    for p in pcts:
        idx = max(0, min(len(sv) - 1, int(round(p * (len(sv) - 1)))))
        out[f"p{int(p*100)}"] = sv[idx]
    return out


def _pair_deltas(rows: list[dict], max_gap_hours: float = 0.2) -> list[dict]:
    """Consecutive Δpkg/Δh pairs only when the gap is small (dense OCR)."""
    deltas = []
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        dh = b["stream_hours"] - a["stream_hours"]
        dp = b["packages"] - a["packages"]
        if 0 < dh <= max_gap_hours and dp >= 0:
            deltas.append({
                "stream_hours": b["stream_hours"],
                "captured_at":  b.get("captured_at"),
                "rate": dp / dh,
                "delta_packages": dp,
                "delta_hours": dh,
                "phase":  b.get("phase"),
                "source": b.get("source"),
                "active_robot": b.get("active_robot"),
            })
    return deltas


# ── 1. Per-phase rates ───────────────────────────────────────────────────────

def rate_by_phase(readings: list[dict]) -> dict:
    """
    For each phase, integrate packages over the time covered by readings in that phase.
    Reports the trustworthy regime separation that the global rate hides.
    """
    by_phase: dict[str, list[dict]] = defaultdict(list)
    for r in readings:
        ph = r.get("phase") or "unknown"
        by_phase[ph].append(r)

    out = {}
    for ph, rows in by_phase.items():
        rows = sorted(rows, key=lambda r: r["stream_hours"])
        if len(rows) < 2:
            continue
        dh = rows[-1]["stream_hours"] - rows[0]["stream_hours"]
        dp = rows[-1]["packages"] - rows[0]["packages"]
        deltas = _pair_deltas(rows)
        rates = [d["rate"] for d in deltas]
        out[ph] = {
            "readings": len(rows),
            "hours_covered": round(dh, 3),
            "packages_in_phase": dp,
            "macro_rate": round(dp / dh, 1) if dh > 0 else None,
            "micro_rate_mean": round(statistics.mean(rates), 1) if rates else None,
            "micro_rate_stddev": round(statistics.stdev(rates), 1) if len(rates) > 1 else None,
            "samples": len(rates),
            "first_hour": rows[0]["stream_hours"],
            "last_hour":  rows[-1]["stream_hours"],
        }
    return out


# ── 2. Rate distribution / consistency ───────────────────────────────────────

def rate_distribution(readings: list[dict], phase: str | None = None) -> dict:
    """
    Stats over OCR-pair rates. If phase=None, considers ocr-source rows globally.
    """
    rows = [r for r in readings if r.get("source") == "ocr"
            and (phase is None or r.get("phase") == phase)]
    deltas = _pair_deltas(rows)
    rates = [d["rate"] for d in deltas]
    if len(rates) < 3:
        return {"available": False, "samples": len(rates), "phase": phase or "all_ocr"}
    mean = statistics.mean(rates)
    sd   = statistics.stdev(rates)
    pcts = _percentiles(rates, [0.10, 0.25, 0.50, 0.75, 0.90])
    return {
        "available": True,
        "phase": phase or "all_ocr",
        "samples": len(rates),
        "mean": round(mean, 1),
        "stddev": round(sd, 1),
        "cv_percent": round(sd / mean * 100, 1) if mean else None,
        "min": round(min(rates), 1),
        "max": round(max(rates), 1),
        **{k: round(v, 1) for k, v in pcts.items() if v is not None},
        "interpretation": _consistency_label(sd / mean * 100 if mean else None),
    }


def _consistency_label(cv_pct: float | None) -> str:
    if cv_pct is None:
        return "unknown"
    if cv_pct < 10:   return "highly consistent"
    if cv_pct < 18:   return "consistent"
    if cv_pct < 30:   return "variable"
    return "highly variable"


# ── 3. Time-of-day pattern ───────────────────────────────────────────────────

def rate_pulse(readings: list[dict]) -> dict:
    """
    Multi-window throughput trend — REWRITTEN for honesty.

    Two deltas reported per window:
      - delta_vs_prev_window: rate vs the equivalent window immediately before it
      - delta_vs_overall:    rate vs the cumulative all-time stream rate

    A window is considered "confident" only if it has >= MIN_CONFIDENT_SAMPLES.
    The headline summary uses the OVERALL-baseline delta (more honest than
    same-length-prior-window which biases toward sparse/dense source mix), and
    weights confident windows by sample count.

    If short-window directions disagree by sign and magnitude, summary becomes
    "mixed" so the UI can show that explicitly instead of forcing rising/falling.
    """
    out = {"current_rate": None, "windows": [], "sparkline": [],
           "summary": "insufficient", "mean_delta_pct": None,
           "overall_baseline_rate": None,
           "note": ""}
    if len(readings) < 3:
        return out

    MIN_CONFIDENT_SAMPLES = 5

    # Sparkline: last 30 dense pair-rates (only kept when reads are < 12 min apart)
    deltas = _pair_deltas(readings)
    out["sparkline"] = [round(d["rate"], 1) for d in deltas[-30:]]

    latest_h = readings[-1]["stream_hours"]
    earliest_h = readings[0]["stream_hours"]
    overall_rate = (readings[-1]["packages"] / latest_h) if latest_h > 0 else None
    out["overall_baseline_rate"] = round(overall_rate, 1) if overall_rate else None

    def _stats_in_window(start_offset_hours: float, end_offset_hours: float):
        """Return (rate, n_samples) for stream-hours in [latest - end, latest - start]."""
        lo = latest_h - end_offset_hours
        hi = latest_h - start_offset_hours
        sel = [r for r in readings if lo <= r["stream_hours"] <= hi]
        if len(sel) < 2:
            return (None, len(sel))
        dh = sel[-1]["stream_hours"] - sel[0]["stream_hours"]
        dp = sel[-1]["packages"] - sel[0]["packages"]
        return (round(dp / dh, 1) if dh > 0 else None, len(sel))

    windows_def = [
        ("5m",    5/60),
        ("15m",   15/60),
        ("1h",    1.0),
        ("6h",    6.0),
        ("today", 24.0),
    ]

    weighted_sum_baseline = 0.0
    weight_total = 0.0

    for label, hours in windows_def:
        rate, n = _stats_in_window(0.0, hours)
        prev_rate, _prev_n = _stats_in_window(hours, hours * 2)

        # Delta vs same-length prior window
        if rate is not None and prev_rate is not None and prev_rate > 0:
            d_prev = (rate - prev_rate) / prev_rate * 100
        else:
            d_prev = None

        # Delta vs overall stream baseline (more honest for short-window pulse)
        if rate is not None and overall_rate and overall_rate > 0:
            d_overall = (rate - overall_rate) / overall_rate * 100
        else:
            d_overall = None

        confident = (n >= MIN_CONFIDENT_SAMPLES)
        # Classification uses delta_vs_overall when confident, else "unknown"
        if not confident or d_overall is None:
            direction = "unknown"
        elif d_overall >= 3:
            direction = "up"
        elif d_overall <= -3:
            direction = "down"
        else:
            direction = "flat"

        if confident and d_overall is not None:
            # Weight by sample count, capped to avoid one window dominating
            w = min(n, 30)
            weighted_sum_baseline += d_overall * w
            weight_total += w

        out["windows"].append({
            "label": label,
            "rate": rate,
            "prev_rate": prev_rate,
            "delta_pct": round(d_prev, 1) if d_prev is not None else None,        # back-compat: vs prev window
            "delta_vs_overall_pct": round(d_overall, 1) if d_overall is not None else None,
            "direction": direction,
            "samples": n,
            "confident": confident,
        })

    # current_rate: first window with enough samples to be meaningful
    out["current_rate_source"] = None
    for w in out["windows"]:
        if w.get("rate") is not None and w.get("confident"):
            out["current_rate"] = w["rate"]
            out["current_rate_source"] = w["label"]
            break
    if out["current_rate"] is None:
        # Fall back to first available window
        for w in out["windows"]:
            if w.get("rate") is not None:
                out["current_rate"] = w["rate"]
                out["current_rate_source"] = w["label"] + " (unconfident)"
                break

    # Summary classification with mixed detection
    if weight_total == 0:
        out["summary"] = "insufficient"
        out["note"] = "Not enough confident windows yet — wait for OCR coverage."
        return out

    mean = weighted_sum_baseline / weight_total
    confident_signs = [w["delta_vs_overall_pct"] for w in out["windows"]
                       if w["confident"] and w["delta_vs_overall_pct"] is not None]
    pos = sum(1 for d in confident_signs if d > 1)
    neg = sum(1 for d in confident_signs if d < -1)

    out["mean_delta_pct"] = round(mean, 1)

    if pos > 0 and neg > 0 and abs(mean) < 5:
        out["summary"] = "mixed"
        out["note"] = f"Short windows disagree (↑{pos} ↓{neg} of {len(confident_signs)}); net {mean:+.1f}% vs overall baseline."
    elif mean >= 3:
        out["summary"] = "rising"
        out["note"] = f"Weighted by sample count; current windows are {mean:+.1f}% above the {overall_rate:.0f}/h overall baseline."
    elif mean <= -3:
        out["summary"] = "falling"
        out["note"] = f"Weighted by sample count; current windows are {mean:+.1f}% below the {overall_rate:.0f}/h overall baseline."
    else:
        out["summary"] = "stable"
        out["note"] = f"Weighted by sample count; current windows are {mean:+.1f}% from the {overall_rate:.0f}/h overall baseline."
    return out


def package_mix_summary(readings: list[dict]) -> dict:
    """
    Aggregate package-visual observations into a live mix.
    Looks for these fields per reading (added by vision OCR section G):
      - visible_packages_count (int)
      - package_colors_json (TEXT, JSON list)
      - package_size_dominant (TEXT)
      - package_materials_json (TEXT, JSON list)
    """
    rows = [r for r in readings if r.get("source") == "ocr"]
    if not rows:
        return {"available": False, "reason": "no OCR readings"}

    color_counts: dict[str, int] = {}
    size_counts:  dict[str, int] = {}
    mat_counts:   dict[str, int] = {}
    total_count_obs = 0
    obs_with_data = 0

    import json as _json
    for r in rows:
        used = False
        n = r.get("visible_packages_count")
        if isinstance(n, (int, float)) and n >= 0:
            total_count_obs += int(n)
            used = True
        try:
            cs = _json.loads(r.get("package_colors_json") or "[]")
            for c in cs:
                if isinstance(c, str) and c:
                    color_counts[c.lower()] = color_counts.get(c.lower(), 0) + 1
                    used = True
        except Exception:
            pass
        size = r.get("package_size_dominant")
        if isinstance(size, str) and size:
            size_counts[size.lower()] = size_counts.get(size.lower(), 0) + 1
            used = True
        try:
            ms = _json.loads(r.get("package_materials_json") or "[]")
            for m in ms:
                if isinstance(m, str) and m:
                    mat_counts[m.lower()] = mat_counts.get(m.lower(), 0) + 1
                    used = True
        except Exception:
            pass
        if used:
            obs_with_data += 1

    if not obs_with_data:
        return {"available": False, "reason": "no package observations yet"}

    return {
        "available": True,
        "observations_with_data": obs_with_data,
        "avg_packages_in_frame": round(total_count_obs / obs_with_data, 1) if obs_with_data else None,
        "by_color":    [{"color": k, "count": v} for k, v in
                        sorted(color_counts.items(), key=lambda x: -x[1])],
        "by_size":     [{"size": k,  "count": v} for k, v in
                        sorted(size_counts.items(),  key=lambda x: -x[1])],
        "by_material": [{"material": k, "count": v} for k, v in
                        sorted(mat_counts.items(),  key=lambda x: -x[1])],
    }


def time_of_day_pattern(readings: list[dict]) -> dict:
    """
    Mean pair-rate aggregated by UTC hour of capture. Surfaces day/night cycles.
    """
    rows = [r for r in readings if r.get("source") == "ocr"]
    deltas = _pair_deltas(rows)
    bucket: dict[int, list[float]] = defaultdict(list)
    for d in deltas:
        try:
            t = datetime.fromisoformat(d["captured_at"])
            bucket[t.hour].append(d["rate"])
        except Exception:
            continue
    hours = []
    for h in range(24):
        vals = bucket.get(h, [])
        if len(vals) >= 2:
            hours.append({
                "utc_hour": h,
                "samples": len(vals),
                "mean": round(statistics.mean(vals), 1),
                "stddev": round(statistics.stdev(vals), 1),
                "min": round(min(vals), 1),
                "max": round(max(vals), 1),
            })
    if not hours:
        return {"available": False, "hours": []}
    best = max(hours, key=lambda x: x["mean"])
    worst = min(hours, key=lambda x: x["mean"])
    return {
        "available": True,
        "hours": hours,
        "best_hour":  {"utc_hour": best["utc_hour"],  "mean": best["mean"]},
        "worst_hour": {"utc_hour": worst["utc_hour"], "mean": worst["mean"]},
        "spread_pct": round((best["mean"] - worst["mean"]) / worst["mean"] * 100, 1)
                       if worst["mean"] else None,
    }


# ── 4. Streaks ───────────────────────────────────────────────────────────────

def streaks(readings: list[dict]) -> dict:
    """
    Longest consecutive run of OCR pair-rates above/below median, plus
    longest "dock-like" dip (rate < 600/h) and longest "peak run" (rate > p75).
    """
    rows = [r for r in readings if r.get("source") == "ocr"]
    deltas = _pair_deltas(rows)
    rates = [d["rate"] for d in deltas]
    if len(rates) < 5:
        return {"available": False, "samples": len(rates)}
    median = statistics.median(rates)
    pcts = _percentiles(rates, [0.25, 0.75])
    p25, p75 = pcts["p25"], pcts["p75"]

    def _longest(pred):
        best_len = best_start = best_end = 0
        cur_len = cur_start = 0
        for i, d in enumerate(deltas):
            if pred(d["rate"]):
                if cur_len == 0:
                    cur_start = i
                cur_len += 1
                if cur_len > best_len:
                    best_len = cur_len
                    best_start = cur_start
                    best_end = i
            else:
                cur_len = 0
        if best_len == 0:
            return None
        return {
            "length_readings": best_len,
            "start_hour": deltas[best_start]["stream_hours"],
            "end_hour":   deltas[best_end]["stream_hours"],
            "duration_hours": round(deltas[best_end]["stream_hours"] -
                                    deltas[best_start]["stream_hours"], 3),
            "mean_rate": round(statistics.mean(d["rate"] for d in deltas[best_start:best_end+1]), 1),
        }

    return {
        "available": True,
        "median_rate": round(median, 1),
        "p25_rate": round(p25, 1),
        "p75_rate": round(p75, 1),
        "longest_above_median": _longest(lambda r: r > median),
        "longest_below_median": _longest(lambda r: r < median),
        "longest_peak_run":     _longest(lambda r: r > p75),
        "longest_dock_dip":     _longest(lambda r: r < 600),
    }


# ── 5. Predictive ETAs ───────────────────────────────────────────────────────

def predictive_etas(readings: list[dict], current_rate: float | None) -> list[dict]:
    """For each milestone, compute stream-hour ETA at the current rate."""
    if not readings or not current_rate or current_rate <= 0:
        return []
    latest = readings[-1]
    out = []
    for m in MILESTONES:
        if m <= latest["packages"]:
            out.append({"milestone": m, "reached": True,
                        "reached_at_hour": _find_milestone_hour(readings, m)})
            continue
        delta_pkg = m - latest["packages"]
        eta_hours_from_now = delta_pkg / current_rate
        out.append({
            "milestone": m,
            "reached": False,
            "current_packages": latest["packages"],
            "current_stream_hours": round(latest["stream_hours"], 3),
            "hours_to_milestone": round(eta_hours_from_now, 2),
            "milestone_stream_hour_eta": round(latest["stream_hours"] + eta_hours_from_now, 2),
            "rate_assumed": round(current_rate, 1),
        })
    return out


def _find_milestone_hour(readings: list[dict], milestone: int) -> float | None:
    """Linear-interpolate the stream hour where cumulative packages first ≥ milestone."""
    for i, r in enumerate(readings):
        if r["packages"] >= milestone:
            if i == 0:
                return r["stream_hours"]
            prev = readings[i - 1]
            ratio = (milestone - prev["packages"]) / max(1, (r["packages"] - prev["packages"]))
            return round(prev["stream_hours"] + ratio *
                         (r["stream_hours"] - prev["stream_hours"]), 3)
    return None


# ── 6. Shift records ─────────────────────────────────────────────────────────

def shift_records(readings: list[dict]) -> list[dict]:
    """Discrete split_challenge episodes (start, end, final scores)."""
    episodes = []
    cur = None
    for r in readings:
        is_split = bool(r.get("is_split"))
        if is_split:
            if cur is None:
                cur = {"first": r, "last": r}
            else:
                cur["last"] = r
        else:
            if cur is not None:
                episodes.append(cur)
                cur = None
    if cur is not None:
        episodes.append(cur)

    out = []
    for ep in episodes:
        a, b = ep["first"], ep["last"]
        dh = b["stream_hours"] - a["stream_hours"]
        dp = b["packages"] - a["packages"]
        h_final = b.get("human_packages") or 0
        r_final = b.get("robot_packages") or 0
        gap = ((h_final - r_final) / h_final * 100) if h_final else None
        out.append({
            "start_stream_hour": a["stream_hours"],
            "end_stream_hour": b["stream_hours"],
            "duration_hours": round(dh, 3),
            "packages_in_shift": dp,
            "macro_rate": round(dp / dh, 1) if dh > 0 else None,
            "final_human_packages": h_final or None,
            "final_robot_packages": r_final or None,
            "final_shift_hours_elapsed": b.get("shift_hours"),
            "gap_percent_final": round(gap, 2) if gap is not None else None,
            "robot_won": (r_final >= h_final) if h_final else None,
        })
    return out


# ── 7. Identity canonicalization ─────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for i, cb in enumerate(b, 1):
        curr = [i]
        for j, ca in enumerate(a, 1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def canonicalize_robot(name: str | None, whitelist: tuple[str, ...] = ROBOT_WHITELIST) -> dict:
    """
    Snap a noisy OCR name to the closest known robot, if within edit distance 1.
    Returns {original, canonical, distance, ambiguous}.
    """
    if not name:
        return {"original": name, "canonical": None, "distance": None, "ambiguous": False}
    n = name.strip().upper()
    if n in whitelist:
        return {"original": name, "canonical": n, "distance": 0, "ambiguous": False}
    best, second = None, None
    for cand in whitelist:
        d = _levenshtein(n, cand)
        if best is None or d < best[1]:
            second = best
            best = (cand, d)
        elif second is None or d < second[1]:
            second = (cand, d)
    if best and best[1] <= 1:
        ambiguous = bool(second and second[1] == best[1])
        return {"original": name, "canonical": best[0],
                "distance": best[1], "ambiguous": ambiguous}
    return {"original": name, "canonical": None, "distance": best[1] if best else None,
            "ambiguous": False}


KNOWN_ROBOT_NAMES = ("ROSE", "BOB", "FRANK", "GARY", "JIM", "ALICE")


def get_achievements(readings: list[dict], analysis: dict | None = None) -> list[dict]:
    """
    Gamification ladder — returns every milestone with a `status` field:
      - "unlocked": already achieved
      - "in_progress": next target in this category, with progress % and ETA
      - "locked": future target

    Each row:
      { id, icon, title, description, tier, status,
        progress_pct (0..100), current_value, target_value,
        eta_hours (only for in_progress), unlocked_at_stream_hour (only when unlocked) }
    """
    if not readings:
        return []
    latest = readings[-1]
    total_pkgs   = latest["packages"]
    total_hours  = latest["stream_hours"]
    current_rate = (analysis or {}).get("overall_rate") or 1240.0

    out: list[dict] = []

    def _ladder(category: str, icon: str, current: float,
                ladder: list[tuple], make_title, make_desc,
                rate_for_eta=None):
        """
        ladder = [(threshold, tier), ...] sorted ascending.
        rate_for_eta = how many `threshold-units per stream-hour` we accumulate
                       (used to compute ETA for the next locked rung).
        """
        next_in_progress_set = False
        for threshold, tier in ladder:
            unlocked = current >= threshold
            pct = min(100.0, (current / threshold * 100)) if threshold else 0
            row = {
                "id": f"{category}_{threshold}",
                "icon": icon,
                "title": make_title(threshold),
                "description": make_desc(threshold),
                "tier": tier,
                "current_value": current,
                "target_value": threshold,
                "progress_pct": round(pct, 1),
            }
            if unlocked:
                row["status"] = "unlocked"
            elif not next_in_progress_set:
                row["status"] = "in_progress"
                if rate_for_eta and rate_for_eta > 0:
                    row["eta_hours"] = round((threshold - current) / rate_for_eta, 2)
                next_in_progress_set = True
            else:
                row["status"] = "locked"
            out.append(row)

    def _find_first_hour_at_packages(threshold: int):
        for r in readings:
            if r["packages"] >= threshold:
                return r["stream_hours"]
        return None

    # ── Package milestones (full ladder, ETA from current_rate) ───────────────
    pkg_ladder = [(1_000, "bronze"), (10_000, "bronze"), (50_000, "silver"),
                  (100_000, "silver"), (150_000, "gold"), (200_000, "gold"),
                  (250_000, "platinum"), (500_000, "platinum"), (1_000_000, "platinum")]
    _ladder("pkg", "📦", total_pkgs, pkg_ladder,
            make_title=lambda t: f"{t:,} packages",
            make_desc=lambda t: f"Stream crosses {t:,} cumulative packages sorted.",
            rate_for_eta=current_rate)
    # Attach exact unlock hour for unlocked ones
    for row in out:
        if row["id"].startswith("pkg_") and row["status"] == "unlocked":
            row["unlocked_at_stream_hour"] = _find_first_hour_at_packages(row["target_value"])

    # ── Stream-duration ladder (rate = 1h per stream-hour) ────────────────────
    stream_ladder = [(24, "bronze"), (48, "bronze"), (72, "silver"), (100, "silver"),
                     (150, "gold"), (200, "gold"), (300, "platinum"), (500, "platinum")]
    _ladder("stream", "⏱", total_hours, stream_ladder,
            make_title=lambda t: f"{t} hours streamed",
            make_desc=lambda t: f"Continuous livestream past {t} hours.",
            rate_for_eta=1.0)
    for row in out:
        if row["id"].startswith("stream_") and row["status"] == "unlocked":
            row["unlocked_at_stream_hour"] = row["target_value"]

    # ── Robot identity ladder (one badge per known robot) ─────────────────────
    seen_robots = set()
    first_hour_per_robot: dict[str, float] = {}
    for r in readings:
        rb = canonicalize_robot(r.get("active_robot"))["canonical"] if r.get("active_robot") else None
        if rb and rb not in seen_robots:
            seen_robots.add(rb)
            first_hour_per_robot[rb] = r["stream_hours"]
    # Only emit "Meet X" achievements for robots WE'VE ACTUALLY OBSERVED.
    # Don't invent placeholders for hypothetical robot names — if we've never
    # seen GARY/JIM/ALICE, we don't show a "next: meet them" badge.
    for robot in sorted(seen_robots):
        out.append({
            "id": f"meet_{robot.lower()}",
            "icon": "🤖",
            "title": f"Meet {robot}",
            "description": f"Robot {robot} (F.03) identified on the foreground chest sticker.",
            "tier": "silver",
            "status": "unlocked",
            "progress_pct": 100.0,
            "current_value": 1,
            "target_value": 1,
            "unlocked_at_stream_hour": first_hour_per_robot.get(robot),
        })
    # Roster aggregate milestones
    roster_size = len(seen_robots)
    for cnt, tier in [(3, "gold"), (5, "platinum"), (6, "platinum")]:
        out.append({
            "id": f"roster_{cnt}",
            "icon": "👥",
            "title": f"{cnt}+ distinct robots tracked",
            "description": f"Vision pipeline confirms {cnt} or more named robots in the roster.",
            "tier": tier,
            "status": "unlocked" if roster_size >= cnt else ("in_progress" if cnt - roster_size <= 2 else "locked"),
            "progress_pct": round(min(100.0, roster_size / cnt * 100), 1),
            "current_value": roster_size,
            "target_value": cnt,
        })

    # ── Peak-rate ladder ──────────────────────────────────────────────────────
    if analysis:
        peak_rate = (analysis.get("peak") or {}).get("peak_rate") or 0
        peak_hour = (analysis.get("peak") or {}).get("peak_hour")
        peak_ladder = [(1_300, "bronze"), (1_500, "silver"),
                       (1_700, "gold"),   (1_900, "platinum"), (2_100, "platinum")]
        _ladder("peak", "🚀", peak_rate, peak_ladder,
                make_title=lambda t: f"Sustained {t:,} pach/h",
                make_desc=lambda t: f"Hold ≥ {t:,} packages/hour over a 30+ minute window.",
                rate_for_eta=None)  # no clean ETA — depends on robot improving
        for row in out:
            if row["id"].startswith("peak_") and row["status"] == "unlocked":
                row["unlocked_at_stream_hour"] = peak_hour

        # ── Human-vs-robot challenge ──────────────────────────────────────────
        shifts = (analysis.get("benchmarks") or {}).get("shift_records") or []
        if shifts:
            best = min(shifts, key=lambda s: abs(s.get("gap_percent_final") or 999))
            gap = abs(best.get("gap_percent_final") or 999)
            challenge_ladder = [(5.0, "silver", "Sub-5% gap to human"),
                                (2.0, "gold",   "Sub-2% gap to human"),
                                (0.0, "platinum", "Robot beat the human")]
            next_in_progress = False
            for thresh, tier, title in challenge_ladder:
                unlocked = gap <= thresh
                pct = max(0, min(100, (1 - (gap - thresh) / 10) * 100)) if not unlocked else 100
                status = "unlocked" if unlocked else ("in_progress" if not next_in_progress else "locked")
                if status == "in_progress":
                    next_in_progress = True
                out.append({
                    "id": f"challenge_{int(thresh*10)}",
                    "icon": "🏆" if thresh == 0 else "🥈" if thresh == 2 else "🥉",
                    "title": title,
                    "description": f"Robot finishes a 9h challenge within {thresh:.1f}% of (or ahead of) the human.",
                    "tier": tier,
                    "status": status,
                    "progress_pct": round(pct, 1),
                    "current_value": gap,
                    "target_value": thresh,
                    "unlocked_at_stream_hour": best.get("end_stream_hour") if unlocked else None,
                })

        # ── Handoffs observed ─────────────────────────────────────────────────
        handoff_cnt = len(analysis.get("handoffs") or [])
        _ladder("handoff", "🔄", handoff_cnt,
                [(1, "bronze"), (5, "silver"), (10, "gold"), (25, "platinum")],
                make_title=lambda t: f"{t} handoff{'s' if t!=1 else ''} observed",
                make_desc=lambda t: f"Live OCR catches {t} robot rotation event{'s' if t!=1 else ''}.",
                rate_for_eta=None)

        # ── OCR coverage ─────────────────────────────────────────────────────
        ocr_reads = sum(1 for r in readings if r.get("source") == "ocr")
        _ladder("ocr", "👁", ocr_reads,
                [(100, "bronze"), (500, "silver"), (1_000, "gold"),
                 (5_000, "platinum"), (10_000, "platinum")],
                make_title=lambda t: f"{t:,} OCR snapshots",
                make_desc=lambda t: f"Vision pipeline captures {t:,} structured frames.",
                rate_for_eta=60.0)  # ~60 OCR per stream-hour at 60s interval

    return out


def normalize_robot_identities(readings: list[dict]) -> dict:
    """Aggregate-level summary of identity quality across the dataset."""
    counts: dict[str, int] = defaultdict(int)
    raw: dict[str, int] = defaultdict(int)
    for r in readings:
        rb = r.get("active_robot")
        if not rb:
            continue
        raw[rb] += 1
        c = canonicalize_robot(rb)
        if c["canonical"]:
            counts[c["canonical"]] += 1
        else:
            counts[f"<unknown:{rb}>"] += 1
    return {
        "by_canonical": dict(sorted(counts.items(), key=lambda x: -x[1])),
        "raw_observations": dict(sorted(raw.items(), key=lambda x: -x[1])),
        "total_with_identity": sum(raw.values()),
    }
