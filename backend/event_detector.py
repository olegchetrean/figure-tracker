"""
Tier-2 vision pipeline — runs gemini-3.1-flash-lite-preview every N minutes
on a freshly-grabbed frame and extracts RICHER events than the fast OCR loop:

  - notable_actions       — interesting things the robot is doing
  - anomalies / scene events — anything visibly out of pattern
  - audience / observers  — humans or other robots watching
  - free-text observation — 1-2 sentence semantic description

Stored in a new SQLite table `vision_events` and surfaced via /api/events.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

import cv2
import numpy as np
from openai import OpenAI

try:
    from cost_tracker import record_call as _record_cost
except ImportError:
    _record_cost = None

log = logging.getLogger(__name__)

DB_PATH          = os.getenv("DB_PATH", "/opt/figure-tracker/data/tracker.db")
EVENT_MODEL      = os.getenv("EVENT_MODEL", "gpt-5.4-nano")  # tier-2 uses same model with richer prompt
                                                              # (Gemini / Claude Haiku not yet whitelisted for this key)
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "https://api.megapromoting.com/v1")
EVENT_INTERVAL_S = int(os.getenv("EVENT_INTERVAL", "600"))    # 10 min default
MAX_TOKENS       = int(os.getenv("EVENT_MAX_TOKENS", "500"))

VALID_EVENT_TYPES = {
    "error", "recovery", "interesting_action", "achievement",
    "scene_change", "handoff_observed", "audience", "anomaly",
}
VALID_SEVERITIES = {"low", "medium", "high"}


# ── Prompt ───────────────────────────────────────────────────────────────────

PROMPT = """You are a vision analyst watching a single frame from the Figure AI warehouse robot YouTube livestream.

Your job is NOT to read numbers (another system does that). Your job is to identify RICH SEMANTIC EVENTS:
something interesting, surprising, anomalous, or worth flagging in this frame.

Look for ANY of these and pick the SINGLE most prominent:

  - "error":              robot made a clear mistake (drop / misplace / jam / freeze / grip slip)
  - "recovery":           robot recovering from a previous mistake (retrieving a fallen package, untangling, etc.)
  - "interesting_action": robot doing something unusual but correct (reaching far, rotating package, two-handed grip, picking oddly-shaped item)
  - "achievement":        a clear milestone (visible counter just crossed a round number, large package volume, peak performance moment)
  - "scene_change":       camera angle / lighting / set design has visibly changed from the typical view
  - "handoff_observed":   a robot rotation visibly in progress (one robot leaving, another entering or already in position)
  - "audience":           humans (operators, staff, visitors) clearly visible in the scene watching
  - "anomaly":            anything visibly unusual that doesn't fit the others (background activity, weird package, equipment fault)

If NOTHING noteworthy is visible (just routine sorting), set `event_type` to null and `severity` to null.

Reply with ONLY a JSON object (no markdown, no code fences, no prose) with these exact keys:

{
  "event_type":   one of the categories above, or null if frame is routine.
  "severity":     "low" | "medium" | "high" (relative interest level), or null.
  "summary":      short 1-sentence description in plain English (≤ 140 chars). Always provide, even when event_type is null (e.g. "Routine sorting; robot working steadily.").
  "observation":  longer 1-2 sentence semantic description of what's visible in the frame (≤ 280 chars). Be specific, not generic.
  "robots_visible_count": integer — how many humanoid robots are visible in frame (foreground + background).
  "humans_visible":  boolean — are any humans clearly visible?
  "interest_score":  number 0..1 — how interesting / unusual this frame is. Routine = 0.0; clear error or milestone = 1.0.
}

Be precise and concrete. Do NOT invent things you cannot see.
"""


# ── DB schema ────────────────────────────────────────────────────────────────

def init_events_db() -> None:
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS vision_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                model TEXT NOT NULL,
                frame_stream_hours REAL,
                frame_packages INTEGER,
                event_type TEXT,
                severity TEXT,
                summary TEXT,
                observation TEXT,
                robots_visible_count INTEGER,
                humans_visible INTEGER,
                interest_score REAL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cost_usd REAL,
                raw_response TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ve_captured_at ON vision_events(captured_at);
            CREATE INDEX IF NOT EXISTS idx_ve_event_type  ON vision_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_ve_interest    ON vision_events(interest_score);
        """)


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=LITELLM_BASE_URL,
    )


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _frame_to_b64_jpg(frame: np.ndarray, quality: int = 80) -> str:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return base64.b64encode(buf).decode("ascii")


# ── Single analysis call ─────────────────────────────────────────────────────

def analyze_frame(frame: np.ndarray,
                  frame_stream_hours: float | None = None,
                  frame_packages: int | None = None) -> dict | None:
    """
    Run gemini-3.1-flash-lite on the given frame and persist the parsed event.
    Returns the stored row dict or None on failure.
    """
    if frame is None or frame.size == 0:
        return None

    try:
        img_b64 = _frame_to_b64_jpg(frame)
    except Exception as e:
        log.error("Event detector: encode failed: %s", e)
        return None

    try:
        resp = _client().chat.completions.create(
            model=EVENT_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{img_b64}",
                        "detail": "high",
                    }},
                ],
            }],
        )
    except Exception as e:
        log.error("Event detector: API call failed: %s", e)
        return None

    raw = (resp.choices[0].message.content or "").strip()
    data = _extract_json(raw) or {}

    # Sanitise + whitelist
    et = data.get("event_type")
    if isinstance(et, str):
        et = et.strip().lower()
        et = et if et in VALID_EVENT_TYPES else None
    else:
        et = None

    sev = data.get("severity")
    if isinstance(sev, str):
        sev = sev.strip().lower()
        sev = sev if sev in VALID_SEVERITIES else None
    else:
        sev = None
    if et is None:
        sev = None  # severity only meaningful with an event

    summary     = (data.get("summary") or "").strip()[:200]
    observation = (data.get("observation") or "").strip()[:400]
    n_robots    = data.get("robots_visible_count")
    n_robots    = int(n_robots) if isinstance(n_robots, (int, float)) else None
    humans      = bool(data.get("humans_visible", False))
    interest    = data.get("interest_score")
    try:
        interest = float(interest)
        interest = max(0.0, min(1.0, interest))
    except Exception:
        interest = None

    # Cost tracking — gemini pricing differs from gpt-5.4-nano
    cost_usd = None
    p_tok = c_tok = None
    try:
        u = resp.usage
        p_tok, c_tok = u.prompt_tokens, u.completion_tokens
        if _record_cost is not None:
            rec = _record_cost(
                model=EVENT_MODEL,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                detail="high",
            )
            cost_usd = rec.get("cost_usd") if rec else None
    except Exception as e:
        log.debug("event-detector cost log failed: %s", e)

    # Persist
    captured_at = datetime.utcnow().isoformat()
    row = {
        "captured_at": captured_at,
        "model": EVENT_MODEL,
        "frame_stream_hours": frame_stream_hours,
        "frame_packages": frame_packages,
        "event_type": et,
        "severity": sev,
        "summary": summary,
        "observation": observation,
        "robots_visible_count": n_robots,
        "humans_visible": int(humans),
        "interest_score": interest,
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "cost_usd": cost_usd,
        "raw_response": raw[:2000],
    }
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO vision_events
                  (captured_at, model, frame_stream_hours, frame_packages,
                   event_type, severity, summary, observation,
                   robots_visible_count, humans_visible, interest_score,
                   prompt_tokens, completion_tokens, cost_usd, raw_response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(row.values()))
            row["id"] = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.Error as e:
        log.error("Event detector: DB write failed: %s", e)
        return None

    log.info("Event [%s] type=%s sev=%s interest=%s — %s",
             EVENT_MODEL, et or "routine", sev or "-",
             f"{interest:.2f}" if interest is not None else "?",
             summary[:80])
    return row


# ── Scheduled poll ───────────────────────────────────────────────────────────

def poll_event() -> dict | None:
    """
    Called by APScheduler. Grabs a fresh frame using the OCR worker's helpers,
    then runs analyze_frame().
    """
    try:
        from ocr_worker import get_stream_url, grab_frame
        from database import get_latest_reading
    except ImportError as e:
        log.error("Event detector: cannot import worker helpers: %s", e)
        return None

    url = get_stream_url()
    if not url:
        log.info("Event detector: stream offline")
        return None
    frame = grab_frame(url)
    if frame is None:
        log.warning("Event detector: empty frame")
        return None

    latest = get_latest_reading() or {}
    return analyze_frame(
        frame,
        frame_stream_hours=latest.get("stream_hours"),
        frame_packages=latest.get("packages"),
    )


# ── Read API ─────────────────────────────────────────────────────────────────

def recent_events(limit: int = 50, only_notable: bool = False) -> list[dict]:
    """Most-recent events, optionally filtered to non-null event_type."""
    try:
        with _conn() as c:
            q = """
                SELECT * FROM vision_events
                {where}
                ORDER BY id DESC LIMIT ?
            """.format(where="WHERE event_type IS NOT NULL" if only_notable else "")
            rows = c.execute(q, (limit,)).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log.error("recent_events failed: %s", e)
        return []


def event_summary() -> dict:
    """Aggregate stats: total events, by_type, by_severity, avg interest, last 24h."""
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM vision_events").fetchone()[0]
            notable = c.execute(
                "SELECT COUNT(*) FROM vision_events WHERE event_type IS NOT NULL"
            ).fetchone()[0]
            by_type = {r["event_type"]: r["n"] for r in c.execute(
                "SELECT event_type, COUNT(*) AS n FROM vision_events "
                "WHERE event_type IS NOT NULL GROUP BY event_type ORDER BY n DESC")}
            by_sev = {r["severity"]: r["n"] for r in c.execute(
                "SELECT severity, COUNT(*) AS n FROM vision_events "
                "WHERE severity IS NOT NULL GROUP BY severity")}
            avg_interest = c.execute(
                "SELECT ROUND(AVG(interest_score), 3) FROM vision_events"
            ).fetchone()[0]
            last_24h = c.execute(
                "SELECT COUNT(*) FROM vision_events "
                "WHERE captured_at >= datetime('now', '-1 day')"
            ).fetchone()[0]
        return {
            "total_observations": total,
            "notable_events": notable,
            "by_type": by_type,
            "by_severity": by_sev,
            "avg_interest_score": avg_interest,
            "observations_last_24h": last_24h,
            "model": EVENT_MODEL,
        }
    except sqlite3.Error as e:
        log.error("event_summary failed: %s", e)
        return {"error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_events_db()
    res = poll_event()
    print(json.dumps(res, indent=2, default=str))
    print(json.dumps(event_summary(), indent=2))
