"""
Runs hourly via APScheduler (started from main.py).
Uses LiteLLM gateway (OpenAI-compatible) with gpt-5.4-nano for cost efficiency.
"""
import os
import logging
from openai import OpenAI

from database import get_all_readings, insert_ai_report
from analysis import full_analysis

log = logging.getLogger(__name__)

MODEL = "gpt-5.4-nano"
MAX_TOKENS = 350


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=os.getenv("LITELLM_BASE_URL", "https://api.megapromoting.com/v1"),
    )


def build_prompt(a: dict) -> str:
    agi = a["agi"]
    rates = a["rates_by_window"]

    gap_str = "robot WINNING" if agi["robot_winning"] else f"human leads by {agi['gap_percent']:.1f}%"
    human_vs = (
        f"ACTIVE — Human {agi['human_rate']:.0f} pach/h vs Robot {agi['robot_rate']:.0f} pach/h ({gap_str})"
        if agi["active"]
        else f"Not active. Robot last 1h: {agi['robot_rate']:.0f} pach/h, "
             f"human baseline: {agi['human_rate']:.0f} pach/h "
             f"(robot is {agi['gap_percent']:.1f}% behind)"
    )

    segs = a.get("hourly_segments", [])
    recent_segs = segs[-5:] if len(segs) >= 5 else segs
    seg_summary = " | ".join(
        f"{s['start_hour']:.0f}→{s['end_hour']:.0f}h: {s['packages_per_hour']:.0f}/h"
        for s in recent_segs
    )

    return f"""You are a concise robotics analyst. Write exactly 3 sentences about this Figure AI robot livestream data.
Cover: (1) is performance stable/improving/declining and why, (2) human vs robot gap, (3) key insight or prediction.
Be factual, no hype.

DATA:
- Stream: {a['total_hours']:.1f}h running, {a['total_packages']:,} packages total
- Overall rate: {a['overall_rate']:.0f} pach/h
- Trend: {a['trend']} (linear slope {a['trend_slope']:+.3f} pach/h per hour of stream time)
- Last 30m: {rates.get('30m')} | 1h: {rates.get('1h')} | 6h: {rates.get('6h')} | 24h: {rates.get('24h')} pach/h
- Recent segments: {seg_summary}
- Dock events: {len(a['dock_events'])}
- Human vs Robot: {human_vs}
- Data points collected: {a['reading_count']}
"""


def generate_report() -> str:
    readings = get_all_readings()
    if not readings:
        return "No data available yet — OCR worker may not have started."

    a = full_analysis(readings)
    prompt = build_prompt(a)

    try:
        client = _client()
        resp = client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        report = resp.choices[0].message.content.strip()
    except Exception as exc:
        log.error("AI analyst error: %s", exc)
        report = f"AI analysis temporarily unavailable."

    insert_ai_report(report)
    log.info("AI report generated (%d chars)", len(report))
    return report
