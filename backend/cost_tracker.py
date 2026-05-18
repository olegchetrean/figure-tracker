"""Cost tracking for vision-LLM calls made by the OCR worker.

Stores per-call token usage in `vision_costs` (same SQLite DB as readings),
computes USD cost from configurable price-per-1M-token constants, and exposes
aggregation/projection helpers consumed by the API layer.

Stdlib only. Idempotent schema init. Fail-soft writes (no exception propagates
from `record_call` — a failing cost log must never break an OCR cycle).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/opt/figure-tracker/data/tracker.db")

# Default prices in USD per 1M tokens. Override at runtime via env vars
# VISION_PRICE_INPUT_PER_1M and VISION_PRICE_OUTPUT_PER_1M (apply to ALL models).
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.4-nano":      {"input": 0.10, "output": 0.40},
    "gpt-5.4":           {"input": 1.00, "output": 4.00},
    "gpt-5.1":           {"input": 1.50, "output": 6.00},
    "gpt-4o-mini":       {"input": 0.15, "output": 0.60},
    "gpt-4.1":           {"input": 2.00, "output": 8.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input": 0.80, "output": 4.00},
}

# Fallback used when neither env override nor per-model entry exists. Conservative
# nano-class pricing keeps surprises small if a new model slips through.
_FALLBACK_PRICING = {"input": 0.10, "output": 0.40}


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------

def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning("cost_tracker: env %s is not a float (%r); ignoring", name, raw)
        return None


def get_pricing(model: str) -> dict:
    """Resolve the pricing for `model`. Env overrides win over per-model defaults."""
    base = DEFAULT_PRICING.get(model, _FALLBACK_PRICING)
    inp_env = _env_float("VISION_PRICE_INPUT_PER_1M")
    out_env = _env_float("VISION_PRICE_OUTPUT_PER_1M")
    return {
        "input": inp_env if inp_env is not None else base["input"],
        "output": out_env if out_env is not None else base["output"],
    }


def cost_for_call(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """Pure helper: compute USD cost for a single call. No DB I/O."""
    pricing = get_pricing(model)
    input_usd = (prompt_tokens or 0) * pricing["input"] / 1_000_000
    output_usd = (completion_tokens or 0) * pricing["output"] / 1_000_000
    return {
        "input_usd": input_usd,
        "output_usd": output_usd,
        "total_usd": input_usd + output_usd,
        "input_price_per_1m": pricing["input"],
        "output_price_per_1m": pricing["output"],
    }


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------

@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_cost_db() -> None:
    """Idempotent — safe to call on every process start."""
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS vision_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                reading_id INTEGER,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vc_captured_at ON vision_costs(captured_at);
            CREATE INDEX IF NOT EXISTS idx_vc_model ON vision_costs(model);
            """
        )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def record_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    reading_id: int | None = None,
    detail: str | None = None,
    captured_at: str | None = None,
) -> dict | None:
    """Persist a single vision call's cost. Fail-soft: returns None on DB error."""
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    total_tokens = prompt_tokens + completion_tokens
    ts = captured_at or datetime.utcnow().isoformat()
    breakdown = cost_for_call(model, prompt_tokens, completion_tokens)
    cost_usd = breakdown["total_usd"]

    try:
        with _conn() as c:
            cur = c.execute(
                """INSERT INTO vision_costs
                   (captured_at, reading_id, model, prompt_tokens, completion_tokens,
                    total_tokens, cost_usd, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, reading_id, model, prompt_tokens, completion_tokens,
                 total_tokens, cost_usd, detail),
            )
            row_id = cur.lastrowid
    except sqlite3.Error as e:
        log.exception("cost_tracker.record_call failed: %s", e)
        return None

    return {
        "id": row_id,
        "captured_at": ts,
        "reading_id": reading_id,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "detail": detail,
        "input_usd": breakdown["input_usd"],
        "output_usd": breakdown["output_usd"],
        "input_price_per_1m": breakdown["input_price_per_1m"],
        "output_price_per_1m": breakdown["output_price_per_1m"],
    }


# ---------------------------------------------------------------------------
# Windowing helpers
# ---------------------------------------------------------------------------

_WINDOWS = {"today", "24h", "7d", "30d", "all"}


def _window_since(window: str) -> datetime | None:
    """Return the lower bound (UTC, naive) for the given window, or None for 'all'."""
    now = datetime.utcnow()
    if window == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "24h":
        return now - timedelta(hours=24)
    if window == "7d":
        return now - timedelta(days=7)
    if window == "30d":
        return now - timedelta(days=30)
    if window == "all":
        return None
    raise ValueError(f"unknown window {window!r}; expected one of {_WINDOWS}")


# ---------------------------------------------------------------------------
# Reads / aggregates
# ---------------------------------------------------------------------------

def aggregate(window: str = "today") -> dict:
    since = _window_since(window)
    where = ""
    params: tuple = ()
    if since is not None:
        where = "WHERE captured_at >= ?"
        params = (since.isoformat(),)

    with _conn() as c:
        totals = c.execute(
            f"""SELECT
                    COUNT(*)                       AS calls,
                    COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                    COALESCE(SUM(cost_usd), 0.0)        AS total_cost_usd
                FROM vision_costs {where}""",
            params,
        ).fetchone()

        per_model = c.execute(
            f"""SELECT model,
                       COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                       COALESCE(SUM(cost_usd), 0.0)        AS cost_usd
                FROM vision_costs {where}
                GROUP BY model
                ORDER BY cost_usd DESC""",
            params,
        ).fetchall()

    calls = int(totals["calls"] or 0)
    total_cost = float(totals["total_cost_usd"] or 0.0)
    avg = (total_cost / calls) if calls else 0.0

    return {
        "window": window,
        "since": since.isoformat() if since else None,
        "calls": calls,
        "total_tokens": int(totals["total_tokens"] or 0),
        "prompt_tokens": int(totals["prompt_tokens"] or 0),
        "completion_tokens": int(totals["completion_tokens"] or 0),
        "total_cost_usd": total_cost,
        "avg_cost_per_call_usd": avg,
        "models": [
            {
                "model": r["model"],
                "calls": int(r["calls"]),
                "prompt_tokens": int(r["prompt_tokens"]),
                "completion_tokens": int(r["completion_tokens"]),
                "total_tokens": int(r["total_tokens"]),
                "cost_usd": float(r["cost_usd"]),
            }
            for r in per_model
        ],
    }


def daily_breakdown(days: int = 14) -> list[dict]:
    """Per-day rollup, most recent first. Includes zero-call days for chart continuity."""
    days = max(int(days), 1)
    today = datetime.utcnow().date()
    start = today - timedelta(days=days - 1)
    start_iso = datetime.combine(start, datetime.min.time()).isoformat()

    with _conn() as c:
        rows = c.execute(
            """SELECT substr(captured_at, 1, 10) AS day,
                      COUNT(*)                       AS calls,
                      COALESCE(SUM(cost_usd), 0.0)   AS cost_usd,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens
               FROM vision_costs
               WHERE captured_at >= ?
               GROUP BY day""",
            (start_iso,),
        ).fetchall()

    by_day = {r["day"]: r for r in rows}
    out: list[dict] = []
    for i in range(days):
        d = today - timedelta(days=i)
        key = d.isoformat()
        r = by_day.get(key)
        out.append({
            "date": key,
            "calls": int(r["calls"]) if r else 0,
            "cost_usd": float(r["cost_usd"]) if r else 0.0,
            "total_tokens": int(r["total_tokens"]) if r else 0,
        })
    return out


def hourly_breakdown(hours: int = 24) -> list[dict]:
    """Last N hours, oldest first (so charts read left-to-right)."""
    hours = max(int(hours), 1)
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=hours - 1)
    start_iso = start.isoformat()

    with _conn() as c:
        rows = c.execute(
            """SELECT substr(captured_at, 1, 13) AS hr,
                      COUNT(*)                       AS calls,
                      COALESCE(SUM(cost_usd), 0.0)   AS cost_usd,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens
               FROM vision_costs
               WHERE captured_at >= ?
               GROUP BY hr""",
            (start_iso,),
        ).fetchall()

    by_hr = {r["hr"]: r for r in rows}
    out: list[dict] = []
    for i in range(hours):
        h = start + timedelta(hours=i)
        # SQLite substr(captured_at, 1, 13) yields "YYYY-MM-DDTHH"
        key = h.isoformat()[:13]
        r = by_hr.get(key)
        out.append({
            "hour": h.isoformat(),
            "calls": int(r["calls"]) if r else 0,
            "cost_usd": float(r["cost_usd"]) if r else 0.0,
            "total_tokens": int(r["total_tokens"]) if r else 0,
        })
    return out


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def projection(window_hours: float = 24, interval_seconds: float = 120) -> dict:
    """Project month/year cost from the most recent `window_hours` of observed usage.

    Falls back to the configured `interval_seconds` cadence when there is no
    observed data yet, so a fresh deployment still shows a useful estimate.
    """
    window_hours = max(float(window_hours), 1e-6)
    since = datetime.utcnow() - timedelta(hours=window_hours)

    with _conn() as c:
        row = c.execute(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(cost_usd), 0.0) AS cost
               FROM vision_costs
               WHERE captured_at >= ?""",
            (since.isoformat(),),
        ).fetchone()

    observed_calls = int(row["calls"] or 0)
    observed_cost = float(row["cost"] or 0.0)

    if observed_calls > 0:
        calls_per_hour = observed_calls / window_hours
        cost_per_hour = observed_cost / window_hours
    else:
        # No observations yet — extrapolate purely from cadence at $0 cost.
        interval_seconds = max(float(interval_seconds), 1.0)
        calls_per_hour = 3600.0 / interval_seconds
        cost_per_hour = 0.0

    cost_per_day = cost_per_hour * 24
    return {
        "based_on_last_hours": window_hours,
        "observed_calls": observed_calls,
        "observed_cost_usd": observed_cost,
        "calls_per_hour": calls_per_hour,
        "cost_per_hour_usd": cost_per_hour,
        "cost_per_day_usd": cost_per_day,
        "cost_per_month_usd_30d": cost_per_day * 30,
        "cost_per_year_usd_365d": cost_per_day * 365,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    init_cost_db()
    print(json.dumps(aggregate("today"), indent=2, default=str))
    print(json.dumps(projection(), indent=2, default=str))
