"""
Template-based narrative generation for the Figure AI Tracker.

Deterministic, reproducible, LLM-free. Takes the output of
`analysis.full_analysis(readings)` plus the raw readings list and emits:

  - master_conclusion(analysis)  → one-shot synthesis (headline + paragraphs
                                    + what-we-know / what-we-don't-know).
  - segment_narratives(readings, analysis) → per-time-segment summaries
                                              labelled relative to the median
                                              segment rate.
  - error_narratives(error_observations)   → forward-compatible placeholder
                                              for the error-pipeline (returns
                                              a sane empty shape today).

All functions are pure (no DB, no network) and use only the standard library.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Any


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_int(n: Any) -> str:
    try:
        return f"{int(round(float(n))):,}"
    except (TypeError, ValueError):
        return "?"


def _fmt_rate(n: Any) -> str:
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_pct(n: Any, *, sign: bool = False) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "?%"
    if sign and v >= 0:
        return f"+{v:.1f}%"
    return f"{v:.1f}%"


def _fmt_hours(n: Any) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "?h"
    if v < 1:
        return f"{v*60:.0f} min"
    return f"{v:.1f}h"


def _confidence_band(conf: float | None) -> str:
    if conf is None:
        return "low"
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return "low"
    if c >= 0.7:
        return "high"
    if c >= 0.4:
        return "medium"
    return "low"


def _readings_in_window(readings: list[dict], start: float, end: float) -> list[dict]:
    return [r for r in readings if start <= r.get("stream_hours", -1) <= end]


def _pairwise_rates_in_window(rows: list[dict]) -> list[dict]:
    out = []
    for i in range(1, len(rows)):
        dh = rows[i]["stream_hours"] - rows[i - 1]["stream_hours"]
        dp = rows[i]["packages"] - rows[i - 1]["packages"]
        if dh > 0 and dp >= 0:
            out.append({
                "hour": rows[i - 1]["stream_hours"],
                "hour_end": rows[i]["stream_hours"],
                "rate": dp / dh,
                "delta_hours": dh,
                "delta_packages": dp,
            })
    return out


def _canonical_robots(rows: list[dict]) -> list[str]:
    """Pull distinct canonical robot identities present in the window."""
    seen: list[str] = []
    for r in rows:
        canon = r.get("active_robot")
        if not canon:
            continue
        canon_norm = str(canon).strip().upper()
        if canon_norm and canon_norm not in seen:
            seen.append(canon_norm)
    return seen


# ── Public function 1: master conclusion ─────────────────────────────────────

def master_conclusion(analysis: dict) -> dict:
    """
    Synthesize the full analysis into a human-readable verdict.

    Returns a dict with `headline`, `paragraphs` (list of sentences),
    `confidence_level`, `what_we_know`, `what_we_dont_know`, `watch_next`.
    """
    if not analysis or analysis.get("error"):
        return {
            "headline": "Not enough data yet to draw conclusions.",
            "paragraphs": [
                "The analysis pipeline did not return a usable dataset.",
                "Capture more readings before re-running the synthesis.",
            ],
            "confidence_level": "low",
            "what_we_know": [],
            "what_we_dont_know": [
                "Almost everything — no readings have been analysed yet.",
            ],
            "watch_next": [
                "Wait for the next OCR poll to fill in the timeline.",
            ],
        }

    verdict = analysis.get("verdict") or {}
    learning = analysis.get("learning") or {}
    benchmarks = analysis.get("benchmarks") or {}
    rate_dist = benchmarks.get("rate_distribution") or {}
    tod = benchmarks.get("time_of_day_pattern") or {}
    shift_records = benchmarks.get("shift_records") or []
    rate_by_phase = benchmarks.get("rate_by_phase") or {}
    streaks = benchmarks.get("streaks") or {}
    peak = analysis.get("peak") or {}
    agi = analysis.get("agi") or {}
    current = analysis.get("current_status") or {}
    phase_timeline = analysis.get("phase_timeline") or []

    total_packages = analysis.get("total_packages", 0)
    total_hours = analysis.get("total_hours", 0)
    overall_rate = analysis.get("overall_rate", 0)
    reading_count = analysis.get("reading_count", 0)

    verdict_title = verdict.get("title") or "INCONCLUSIVE — performance is stable, no clear trend"
    verdict_class = verdict.get("verdict_class") or "neutral"
    verdict_conf = verdict.get("confidence", 0.0)

    # ── Headline ────────────────────────────────────────────────────────────
    nuance_map = {
        "positive": "moderate signal of improvement",
        "negative": "moderate signal of decline",
        "neutral":  "no clear trend",
    }
    nuance = nuance_map.get(verdict_class, "unclear signal")
    headline = (
        f"{verdict_title} — {nuance} across "
        f"{_fmt_int(total_packages)} packages over {_fmt_hours(total_hours)} "
        f"of stream ({reading_count} readings)."
    )

    # ── Paragraphs ──────────────────────────────────────────────────────────
    paragraphs: list[str] = []

    # 1) Throughput
    p1 = (
        f"Overall throughput sits at {_fmt_rate(overall_rate)} pach/h across "
        f"{_fmt_hours(total_hours)} of stream "
        f"({_fmt_int(total_packages)} packages cumulative)."
    )
    if learning.get("available"):
        p1 += (
            f" Comparing the first {learning.get('window_hours', '?')}h to the last "
            f"{learning.get('window_hours', '?')}h, the rate moved from "
            f"{_fmt_rate(learning.get('early_rate'))} to "
            f"{_fmt_rate(learning.get('recent_rate'))} pach/h "
            f"({_fmt_pct(learning.get('delta_percent'), sign=True)})."
        )
    else:
        reason = learning.get("reason") or "insufficient density"
        p1 += f" A time-equal early-vs-recent comparison is not yet available ({reason})."
    paragraphs.append(p1)

    # 2) Challenge result / robot vs human
    if shift_records:
        sr = shift_records[0]
        winner = "human won" if not sr.get("robot_won") else "robot won"
        p2 = (
            f"In the {_fmt_hours(sr.get('duration_hours'))} split-challenge shift, "
            f"the {winner} by {_fmt_pct(sr.get('gap_percent_final'))} "
            f"(robot {_fmt_int(sr.get('final_robot_packages'))} pkgs at "
            f"{_fmt_rate(sr.get('macro_rate'))} pach/h vs human "
            f"{_fmt_int(sr.get('final_human_packages'))} pkgs)."
        )
    elif agi.get("active"):
        gap = agi.get("gap_percent", 0)
        ahead = "ahead" if agi.get("robot_winning") else "behind"
        p2 = (
            f"Live challenge in progress: robot is {_fmt_pct(abs(gap))} {ahead} "
            f"({_fmt_rate(agi.get('robot_rate'))} vs "
            f"{_fmt_rate(agi.get('human_rate'))} pach/h)."
        )
    else:
        p2 = (
            f"No live split-screen challenge is currently active; the most recent "
            f"1-hour rate of {_fmt_rate(agi.get('robot_rate'))} pach/h is "
            f"{_fmt_pct(abs(agi.get('gap_percent', 0)))} "
            f"{'above' if agi.get('robot_winning') else 'below'} the "
            f"1,431 pach/h research baseline for human pickers."
        )
    paragraphs.append(p2)

    # 3) Consistency + time-of-day
    if rate_dist.get("available"):
        cv = rate_dist.get("cv_percent")
        interp = rate_dist.get("interpretation") or "?"
        p3 = (
            f"Per-OCR-pair rates are {interp} "
            f"(CV {_fmt_pct(cv)} across {rate_dist.get('samples', 0)} samples, "
            f"mean {_fmt_rate(rate_dist.get('mean'))} pach/h)."
        )
    else:
        p3 = "Rate-distribution stats are not yet available (need more OCR samples)."
    if tod.get("available"):
        best = tod.get("best_hour") or {}
        worst = tod.get("worst_hour") or {}
        p3 += (
            f" Time-of-day pattern shows a {_fmt_pct(tod.get('spread_pct'))} "
            f"spread between best UTC hour {best.get('utc_hour')} "
            f"({_fmt_rate(best.get('mean'))} pach/h) and worst UTC hour "
            f"{worst.get('utc_hour')} ({_fmt_rate(worst.get('mean'))} pach/h)."
        )
    paragraphs.append(p3)

    # 4) What we know vs what we don't (one-sentence framing)
    p4_bits: list[str] = []
    if peak.get("available"):
        p4_bits.append(
            f"the peak sustained rate is {_fmt_rate(peak.get('peak_rate'))} pach/h "
            f"at stream hour {peak.get('peak_hour'):.1f}"
        )
    if current.get("available") and current.get("active_robot"):
        p4_bits.append(
            f"current active robot is {current.get('active_robot')} "
            f"({current.get('robot_model') or 'model unknown'})"
        )
    if rate_by_phase:
        named = sorted(
            ((ph, d.get("macro_rate") or 0, d.get("readings", 0))
             for ph, d in rate_by_phase.items()),
            key=lambda x: -x[2],
        )
        if named:
            ph, rate, _ = named[0]
            p4_bits.append(
                f"the {ph} phase carries the densest sample at "
                f"{_fmt_rate(rate)} pach/h"
            )
    if p4_bits:
        p4 = "What we know with confidence: " + "; ".join(p4_bits) + "."
    else:
        p4 = "Few hard facts beyond the cumulative totals — most metrics need more samples."
    paragraphs.append(p4)

    # 5) What to watch next
    watch_bits: list[str] = []
    if not learning.get("available"):
        watch_bits.append(
            "a learning-velocity comparison once OCR density covers a longer span"
        )
    if not shift_records:
        watch_bits.append("the next split-screen challenge to re-test robot vs human")
    if tod.get("available") and tod.get("spread_pct", 0) > 20:
        watch_bits.append(
            f"whether the {_fmt_pct(tod.get('spread_pct'))} time-of-day spread persists or flattens"
        )
    if verdict_class == "neutral":
        watch_bits.append("a clearer directional signal (current verdict is INCONCLUSIVE)")
    if not watch_bits:
        watch_bits.append("the next 6h of OCR data to confirm or break the current trend")
    p5 = "Worth watching next: " + "; ".join(watch_bits[:3]) + "."
    paragraphs.append(p5)

    # ── what_we_know ────────────────────────────────────────────────────────
    what_we_know: list[str] = []
    what_we_know.append(
        f"{_fmt_int(total_packages)} packages sorted over {_fmt_hours(total_hours)} "
        f"of stream → overall rate {_fmt_rate(overall_rate)} pach/h."
    )
    if reading_count:
        what_we_know.append(
            f"{reading_count} timeline readings ingested "
            f"(verdict confidence {verdict_conf:.2f})."
        )
    if peak.get("available"):
        what_we_know.append(
            f"Peak sustained rate: {_fmt_rate(peak.get('peak_rate'))} pach/h "
            f"at stream hour {peak.get('peak_hour'):.1f} "
            f"(window {peak.get('peak_span', 0):.1f}h)."
        )
    if rate_dist.get("available"):
        what_we_know.append(
            f"OCR-rate distribution: mean {_fmt_rate(rate_dist.get('mean'))} pach/h, "
            f"CV {_fmt_pct(rate_dist.get('cv_percent'))} "
            f"→ {rate_dist.get('interpretation', '?')} "
            f"({rate_dist.get('samples', 0)} samples)."
        )
    if shift_records:
        sr = shift_records[0]
        outcome = "robot won" if sr.get("robot_won") else "human won"
        what_we_know.append(
            f"Shift record: {_fmt_hours(sr.get('duration_hours'))} challenge, "
            f"{outcome} by {_fmt_pct(sr.get('gap_percent_final'))} "
            f"(robot {_fmt_int(sr.get('final_robot_packages'))} vs human "
            f"{_fmt_int(sr.get('final_human_packages'))})."
        )
    if tod.get("available"):
        what_we_know.append(
            f"Time-of-day spread: {_fmt_pct(tod.get('spread_pct'))} between "
            f"best UTC hour {tod.get('best_hour', {}).get('utc_hour')} and "
            f"worst UTC hour {tod.get('worst_hour', {}).get('utc_hour')}."
        )
    if agi.get("gap_percent") is not None and not agi.get("active"):
        side = "above" if agi.get("robot_winning") else "below"
        what_we_know.append(
            f"Robot vs research-human baseline (1,431 pach/h): currently "
            f"{_fmt_pct(abs(agi.get('gap_percent', 0)))} {side}."
        )
    if current.get("available") and current.get("active_robot"):
        what_we_know.append(
            f"Active robot right now: {current.get('active_robot')} "
            f"({current.get('robot_model') or 'model unknown'}, "
            f"phase: {current.get('phase')})."
        )
    what_we_know = what_we_know[:6]

    # ── what_we_dont_know ───────────────────────────────────────────────────
    what_we_dont_know: list[str] = []
    if not learning.get("available"):
        what_we_dont_know.append(
            f"Whether the robot is meaningfully improving — learning-velocity "
            f"comparison is unavailable ({learning.get('reason', 'insufficient density')})."
        )
    has_unknown_phase = any(s.get("phase") == "unknown" for s in phase_timeline)
    if has_unknown_phase:
        what_we_dont_know.append(
            "Source quality is mixed in the early stream: journalist estimates "
            "and historical OCR are blended with no robot identity, so "
            "comparisons to that period are biased."
        )
    if len(shift_records) <= 1:
        what_we_dont_know.append(
            "Robot-vs-human performance generalises from a single shift record "
            "— one sample, no error bars."
        )
    if tod.get("available") and tod.get("spread_pct", 0) > 20:
        what_we_dont_know.append(
            f"Why the time-of-day spread is {_fmt_pct(tod.get('spread_pct'))} — "
            "we observe the pattern but cannot yet attribute it (lighting? shift "
            "rotation? sample bias?)."
        )
    if rate_dist.get("available") and rate_dist.get("samples", 0) < 100:
        what_we_dont_know.append(
            f"How stable the rate distribution is beyond the current "
            f"{rate_dist.get('samples', 0)} OCR samples."
        )
    if verdict_conf < 0.7:
        what_we_dont_know.append(
            f"How robust the verdict is — current confidence is only {verdict_conf:.2f} "
            "(scales with reading count, capped at 60)."
        )
    if not what_we_dont_know:
        what_we_dont_know.append(
            "No major blind spots flagged — but absence of negative evidence "
            "is not the same as positive evidence."
        )

    # ── watch_next ──────────────────────────────────────────────────────────
    watch_next: list[str] = []
    if not learning.get("available"):
        watch_next.append(
            "Enough OCR density in the early stream to enable a time-equal "
            "early-vs-recent comparison."
        )
    if not shift_records:
        watch_next.append(
            "A second split-screen challenge — would let us put error bars on "
            "the human-vs-robot gap."
        )
    if streaks.get("available"):
        below = streaks.get("longest_below_median") or {}
        if below.get("duration_hours", 0) > 1:
            watch_next.append(
                f"A sustained run above the median rate of "
                f"{_fmt_rate(streaks.get('median_rate'))} pach/h longer than the "
                f"current {_fmt_hours(below.get('duration_hours'))} below-median dip."
            )
    if verdict_class == "neutral":
        watch_next.append(
            "A throughput swing of more than ±5% would flip the verdict out of "
            "INCONCLUSIVE."
        )
    if not watch_next:
        watch_next.append(
            "Continued OCR capture to keep the verdict confidence above 0.7."
        )
    watch_next = watch_next[:4]

    return {
        "headline": headline,
        "paragraphs": paragraphs,
        "confidence_level": _confidence_band(verdict_conf),
        "what_we_know": what_we_know,
        "what_we_dont_know": what_we_dont_know,
        "watch_next": watch_next,
    }


# ── Public function 2: per-segment narratives ────────────────────────────────

def _merge_short_segments(
    timeline: list[dict],
    min_duration_hours: float = 0.5,
) -> list[dict]:
    """
    Collapse consecutive same-phase segments shorter than ``min_duration_hours``.

    The phase-timeline already collapses adjacent same-phase rows, but the
    spec asks us to merge consecutive same-phase chunks that *together* are
    < 30 min. In practice each row of phase_timeline is already a single phase
    run, so we mostly just normalise the shape here — but we still handle the
    case where two adjacent same-phase rows could appear (defensive).
    """
    if not timeline:
        return []
    merged: list[dict] = []
    for seg in timeline:
        if merged and merged[-1]["phase"] == seg.get("phase") and (
            merged[-1].get("duration_hours", 0) < min_duration_hours
            or seg.get("duration_hours", 0) < min_duration_hours
        ):
            tail = merged[-1]
            tail["end_hour"] = seg.get("end_hour", tail["end_hour"])
            tail["end_packages"] = seg.get("end_packages", tail.get("end_packages", 0))
            tail["duration_hours"] = round(tail["end_hour"] - tail["start_hour"], 3)
            tail["packages_in_segment"] = (
                tail["end_packages"] - tail.get("start_packages", 0)
            )
            tail["rate"] = (
                round(tail["packages_in_segment"] / tail["duration_hours"], 1)
                if tail["duration_hours"] > 0 else None
            )
            tail["readings"] = tail.get("readings", 0) + seg.get("readings", 0)
        else:
            merged.append(dict(seg))
    return merged


def _classify_rate(rate: float | None, median_rate: float | None) -> str:
    if rate is None or median_rate is None or median_rate <= 0:
        return "near average"
    ratio = rate / median_rate
    if ratio > 1.10:
        return "peak"
    if ratio > 1.03:
        return "above average"
    if ratio < 0.85:
        return "slowest"
    if ratio < 0.97:
        return "below average"
    return "near average"


def _label_for_segment(seg: dict, index: int, total: int) -> str:
    phase = seg.get("phase") or "unknown"
    start = seg.get("start_hour", 0)
    end = seg.get("end_hour", 0)

    if phase == "split_challenge":
        return f"Challenge: split-screen (stream h{start:.1f}–{end:.1f})"
    if phase == "solo_active":
        return f"Solo run (stream h{start:.1f}–{end:.1f})"
    if phase == "transition":
        return f"Transition (stream h{start:.1f}–{end:.1f})"
    if phase == "unknown":
        day = max(1, int(start // 24) + 1)
        return f"Day {day} (research / mixed sources, h{start:.1f}–{end:.1f})"
    return f"{phase} (stream h{start:.1f}–{end:.1f})"


def segment_narratives(readings: list[dict], analysis: dict) -> list[dict]:
    """
    Break the stream into meaningful time chunks and write a 1–2 sentence
    narrative per chunk. Oldest first, limited to the most recent ~12 chunks.
    """
    if not analysis:
        return []

    phase_timeline = analysis.get("phase_timeline") or []
    if not phase_timeline:
        return []

    merged = _merge_short_segments(phase_timeline, min_duration_hours=0.5)

    # Limit to most-recent 12 segments (oldest-first ordering preserved)
    if len(merged) > 12:
        merged = merged[-12:]

    rates = [s.get("rate") for s in merged if s.get("rate") is not None]
    median_rate = median(rates) if rates else None

    handoffs = analysis.get("handoffs") or []
    benchmarks = analysis.get("benchmarks") or {}
    shift_records = benchmarks.get("shift_records") or []

    total = len(merged)
    out: list[dict] = []

    for i, seg in enumerate(merged):
        start_h = seg.get("start_hour", 0)
        end_h = seg.get("end_hour", 0)
        dur = seg.get("duration_hours", round(end_h - start_h, 3))
        rate = seg.get("rate")
        packages = seg.get("packages_in_segment", 0)
        phase = seg.get("phase") or "unknown"

        rows = _readings_in_window(readings or [], start_h, end_h)
        robots = _canonical_robots(rows)

        # Peak / low rates inside the segment
        pair_rates = _pairwise_rates_in_window(rows)
        seg_peak = max((p["rate"] for p in pair_rates), default=None)
        seg_low = min((p["rate"] for p in pair_rates), default=None)

        # Notable events: handoffs inside the segment + dock-like dips
        notable: list[dict] = []
        for h in handoffs:
            hh = h.get("at_stream_hour")
            if hh is None:
                continue
            if start_h <= hh <= end_h:
                notable.append({
                    "at_stream_hour": round(hh, 2),
                    "type": "handoff",
                    "detail": f"{h.get('from_robot', '?')} → {h.get('to_robot', '?')}",
                })

        # Dock-like dips: sustained <600/h for >10 min
        for p in pair_rates:
            if p["rate"] < 600 and p["delta_hours"] >= (10 / 60):
                notable.append({
                    "at_stream_hour": round(p["hour"], 2),
                    "type": "dock_dip",
                    "detail": (
                        f"rate dropped to {_fmt_rate(p['rate'])} pach/h "
                        f"for {_fmt_hours(p['delta_hours'])}"
                    ),
                })

        # If this segment IS a split_challenge, attach its shift record end-event
        matching_shift = None
        if phase == "split_challenge":
            for sr in shift_records:
                if (sr.get("start_stream_hour") is not None
                        and abs(sr["start_stream_hour"] - start_h) < 0.5):
                    matching_shift = sr
                    break
            if matching_shift:
                outcome = "Robot won" if matching_shift.get("robot_won") else "Human won"
                notable.append({
                    "at_stream_hour": round(matching_shift.get("end_stream_hour", end_h), 2),
                    "type": "challenge_end",
                    "detail": f"{outcome} by {_fmt_pct(matching_shift.get('gap_percent_final'))}",
                })

        # Phase-transition marker (segment boundary)
        if i + 1 < total:
            nxt = merged[i + 1]
            if nxt.get("phase") != phase:
                notable.append({
                    "at_stream_hour": round(end_h, 2),
                    "type": "phase_transition",
                    "detail": f"{phase} → {nxt.get('phase')}",
                })

        classification = _classify_rate(rate, median_rate)

        # ── Narrative text ─────────────────────────────────────────────────
        narrative_bits: list[str] = []

        # Always-on: rate comparison
        if rate is None or median_rate is None:
            narrative_bits.append(
                f"Rate not computable for this {_fmt_hours(dur)} window."
            )
        else:
            pct_vs_median = (rate - median_rate) / median_rate * 100
            sign = "+" if pct_vs_median >= 0 else ""
            narrative_bits.append(
                f"Rate {_fmt_rate(rate)} pach/h ({classification}, "
                f"{sign}{pct_vs_median:.1f}% vs median "
                f"{_fmt_rate(median_rate)} pach/h) over {_fmt_hours(dur)}."
            )

        if phase == "split_challenge" and matching_shift:
            outcome = "robot won" if matching_shift.get("robot_won") else "human won"
            narrative_bits.append(
                f"{_fmt_hours(matching_shift.get('duration_hours'))} split-screen "
                f"challenge: robot {_fmt_int(matching_shift.get('final_robot_packages'))} "
                f"vs human {_fmt_int(matching_shift.get('final_human_packages'))} "
                f"({outcome} by {_fmt_pct(matching_shift.get('gap_percent_final'))})."
            )
        elif phase == "solo_active":
            if robots:
                narrative_bits.append(
                    f"Solo run by {', '.join(robots)} for {_fmt_hours(dur)} "
                    f"({_fmt_int(packages)} packages)."
                )
            else:
                narrative_bits.append(
                    f"Solo run, robot identity not captured "
                    f"({_fmt_int(packages)} packages)."
                )
        elif phase == "unknown":
            narrative_bits.append(
                "Source quality is mixed/low (journalist estimates + historical OCR "
                "with no robot identity); comparisons to later periods are biased."
            )
        elif phase == "transition":
            narrative_bits.append(
                f"Transition window with limited readings "
                f"({seg.get('readings', '?')} samples) — limited insight."
            )

        if notable:
            event_types = Counter(e["type"] for e in notable)
            type_phrase = ", ".join(
                f"{n} {t.replace('_', ' ')}{'s' if n > 1 else ''}"
                for t, n in event_types.items()
            )
            narrative_bits.append(f"Notable: {type_phrase}.")

        narrative = " ".join(narrative_bits)

        out.append({
            "label": _label_for_segment(seg, i, total),
            "phase": phase,
            "start_stream_hour": round(start_h, 2),
            "end_stream_hour": round(end_h, 2),
            "duration_hours": round(dur, 2),
            "packages_in_segment": packages,
            "rate": round(rate, 1) if rate is not None else None,
            "rate_classification": classification,
            "robots_seen": robots,
            "segment_peak_rate": round(seg_peak, 1) if seg_peak is not None else None,
            "segment_low_rate": round(seg_low, 1) if seg_low is not None else None,
            "narrative": narrative,
            "notable_events": notable,
        })

    return out


# ── Public function 3: error narratives (forward-compatible) ─────────────────

def error_narratives(error_observations: list[dict] | None) -> dict:
    """
    Forward-compatible error pipeline summariser.

    Today: returns a placeholder shape so callers can wire it in safely.
    When ``error_observations`` is non-empty, aggregates by type & severity
    and produces a short narrative.
    """
    if not error_observations:
        return {
            "total_errors": 0,
            "errors_per_hour": 0.0,
            "by_type": {},
            "recent_errors": [],
            "narrative": (
                "Error observation pipeline not yet active; no robot mistakes "
                "have been logged."
            ),
        }

    by_type: dict[str, int] = defaultdict(int)
    by_severity: dict[str, int] = defaultdict(int)
    stream_hours_seen: list[float] = []

    for obs in error_observations:
        t = obs.get("error_type") or "unknown"
        sev = obs.get("severity") or "unknown"
        by_type[t] += 1
        by_severity[sev] += 1
        sh = obs.get("stream_hours")
        if isinstance(sh, (int, float)):
            stream_hours_seen.append(float(sh))

    total = len(error_observations)
    span = (max(stream_hours_seen) - min(stream_hours_seen)) if stream_hours_seen else 0
    rate = round(total / span, 2) if span > 0 else 0.0

    # Most-recent 5 by stream_hours, falling back to list order
    sorted_obs = sorted(
        error_observations,
        key=lambda o: o.get("stream_hours") or 0.0,
        reverse=True,
    )
    recent = [
        {
            "stream_hours": o.get("stream_hours"),
            "captured_at": o.get("captured_at"),
            "error_type": o.get("error_type"),
            "severity": o.get("severity"),
            "description": o.get("description"),
        }
        for o in sorted_obs[:5]
    ]

    top_types = ", ".join(
        f"{n} {t}" for t, n in sorted(by_type.items(), key=lambda x: -x[1])[:3]
    ) or "no categorised types"
    top_sev = ", ".join(
        f"{n} {s}" for s, n in sorted(by_severity.items(), key=lambda x: -x[1])[:3]
    ) or "no severity data"

    narrative = (
        f"{total} robot error{'s' if total != 1 else ''} logged across "
        f"{_fmt_hours(span)} of stream ({rate}/h). "
        f"Breakdown — types: {top_types}; severities: {top_sev}."
    )

    return {
        "total_errors": total,
        "errors_per_hour": rate,
        "by_type": dict(by_type),
        "by_severity": dict(by_severity),
        "recent_errors": recent,
        "narrative": narrative,
    }
