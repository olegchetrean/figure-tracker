"""
Provenance / audit database for the Figure AI Tracker.

This module maintains a parallel SQLite database (provenance.db) that records,
for every readings.id, a full snapshot of the metrics it produced plus the
deltas relative to the previous snapshot. Snapshots are append-only and let us
later answer "what did this reading actually change?" and replay any past
analysis state forensically.

The module is self-contained: it only depends on the standard library.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

# ── Configuration ───────────────────────────────────────────────────────────

PROVENANCE_DB_PATH = os.getenv(
    "PROVENANCE_DB_PATH", "/opt/figure-tracker/data/provenance.db"
)

# Weight applied to each ingest source when computing confidence in
# source_quality_log. OCR pipelines are most trusted; manual research notes
# carry only partial weight.
SOURCE_WEIGHTS = {
    "research":   0.5,
    "screenshot": 0.85,
    "ocr_manual": 0.9,
    "ocr":        1.0,
}

# Metrics tracked by record_influence when the caller does not pass an
# explicit list. These names must match keys produced by _extract_metric.
DEFAULT_INFLUENCE_METRICS = [
    "total_packages",
    "overall_rate",
    "recent_1h_rate",
    "peak_rate",
    "learning_delta_pct",
    "verdict_score",
    "agi_robot_rate",
    "agi_gap_pct",
]

# A change is considered significant when either:
#   * the absolute percent change exceeds this threshold, OR
#   * the metric transitioned from undefined to defined (or vice-versa).
SIGNIFICANCE_PCT_THRESHOLD = 0.5


# ── Connection helpers ─────────────────────────────────────────────────────

@contextmanager
def _get_conn():
    """Yield a sqlite3 connection with Row factory; commit on success."""
    db_dir = os.path.dirname(PROVENANCE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(PROVENANCE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.utcnow().isoformat()


# ── Schema ─────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id          INTEGER NOT NULL,
    captured_at         TEXT    NOT NULL,
    total_packages      INTEGER,
    total_hours         REAL,
    overall_rate        REAL,
    recent_1h_rate      REAL,
    peak_rate           REAL,
    peak_hour           REAL,
    learning_delta_pct  REAL,
    verdict_score       INTEGER,
    verdict             TEXT,
    agi_human_rate      REAL,
    agi_robot_rate      REAL,
    agi_gap_pct         REAL,
    reading_count       INTEGER,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_reading_id
    ON metric_snapshots(reading_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_captured_at
    ON metric_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS metric_influence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id      INTEGER NOT NULL,
    metric_name     TEXT    NOT NULL,
    value_before    REAL,
    value_after     REAL,
    delta           REAL,
    pct_change      REAL,
    is_significant  INTEGER,
    computed_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_influence_reading_id
    ON metric_influence(reading_id);
CREATE INDEX IF NOT EXISTS idx_influence_metric_name
    ON metric_influence(metric_name);
CREATE INDEX IF NOT EXISTS idx_influence_reading_metric
    ON metric_influence(reading_id, metric_name);

CREATE TABLE IF NOT EXISTS source_quality_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source                TEXT    NOT NULL UNIQUE,
    reading_count         INTEGER NOT NULL,
    hours_covered         REAL,
    packages_contributed  INTEGER,
    latest_reading_id     INTEGER,
    confidence_score      REAL,
    updated_at            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_quality_source
    ON source_quality_log(source);
"""


def init_provenance_db() -> None:
    """Create the provenance schema if it does not already exist (idempotent)."""
    with _get_conn() as conn:
        conn.executescript(_SCHEMA)


# ── Metric extraction ──────────────────────────────────────────────────────

# Maps the flat metric names we use in snapshots/influence rows to the
# dotted-path inside a full_analysis() dict. Keys whose value is a plain
# string with no dots are treated as top-level keys.
_METRIC_PATHS = {
    "total_packages":      "total_packages",
    "total_hours":         "total_hours",
    "overall_rate":        "overall_rate",
    "recent_1h_rate":      "rates_by_window.1h",
    "peak_rate":           "peak.peak_rate",
    "peak_hour":           "peak.peak_hour",
    "learning_delta_pct":  "learning.delta_percent",
    "verdict_score":       "verdict.score",
    "verdict":             "verdict.verdict",
    "agi_human_rate":      "agi.human_rate",
    "agi_robot_rate":      "agi.robot_rate",
    "agi_gap_pct":         "agi.gap_percent",
    "reading_count":       "reading_count",
}


def _walk(obj, path: str):
    """Walk a dotted path through a nested dict, returning None if any
    segment is missing or yields a non-dict at an interior step."""
    if obj is None:
        return None
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _extract_metric(analysis_dict: dict, metric_name: str):
    """
    Pull a single metric value out of a full_analysis() dict.

    Supports:
      * names registered in _METRIC_PATHS (preferred), OR
      * arbitrary dotted-path keys ("agi.robot_rate") for ad-hoc lookups.

    Returns the raw value (typically float / int / str) or None if missing.
    Never raises on bad input.
    """
    if not isinstance(analysis_dict, dict):
        return None

    path = _METRIC_PATHS.get(metric_name, metric_name)
    value = _walk(analysis_dict, path)

    # Filter out sentinel "not available" payloads (e.g. {"available": False})
    if isinstance(value, dict):
        return None
    return value


def _to_float(value):
    """Coerce to float when possible, otherwise return None.
    Booleans are intentionally rejected (we don't want True → 1.0 surprises)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Snapshots ──────────────────────────────────────────────────────────────

def save_snapshot(reading_id: int, analysis: dict) -> int:
    """
    Persist a metric_snapshots row keyed to reading_id and return its new id.

    The full analysis dict is also stored as JSON so that any future field we
    forgot to materialize into a column can still be recovered.
    """
    snapshot = {
        "reading_id":         int(reading_id),
        "captured_at":        _now(),
        "total_packages":     _extract_metric(analysis, "total_packages"),
        "total_hours":        _to_float(_extract_metric(analysis, "total_hours")),
        "overall_rate":       _to_float(_extract_metric(analysis, "overall_rate")),
        "recent_1h_rate":     _to_float(_extract_metric(analysis, "recent_1h_rate")),
        "peak_rate":          _to_float(_extract_metric(analysis, "peak_rate")),
        "peak_hour":          _to_float(_extract_metric(analysis, "peak_hour")),
        "learning_delta_pct": _to_float(_extract_metric(analysis, "learning_delta_pct")),
        "verdict_score":      _extract_metric(analysis, "verdict_score"),
        "verdict":            _extract_metric(analysis, "verdict"),
        "agi_human_rate":     _to_float(_extract_metric(analysis, "agi_human_rate")),
        "agi_robot_rate":     _to_float(_extract_metric(analysis, "agi_robot_rate")),
        "agi_gap_pct":        _to_float(_extract_metric(analysis, "agi_gap_pct")),
        "reading_count":      _extract_metric(analysis, "reading_count"),
        "snapshot_json":      json.dumps(analysis, default=str),
    }

    # Cast integer-shaped fields explicitly so SQLite stores them as INTEGER.
    for k in ("total_packages", "verdict_score", "reading_count"):
        v = snapshot[k]
        snapshot[k] = int(v) if isinstance(v, (int, float)) and v is not None else None

    cols = list(snapshot.keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO metric_snapshots ({', '.join(cols)}) VALUES ({placeholders})"

    with _get_conn() as conn:
        cur = conn.execute(sql, [snapshot[c] for c in cols])
        return int(cur.lastrowid)


def get_snapshot(reading_id: int) -> dict | None:
    """Return the most recent snapshot for reading_id, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM metric_snapshots
               WHERE reading_id = ?
               ORDER BY id DESC LIMIT 1""",
            (int(reading_id),),
        ).fetchone()
        return dict(row) if row else None


def get_snapshots_range(start_reading_id: int, end_reading_id: int) -> list[dict]:
    """Return snapshots whose reading_id is within [start, end] inclusive,
    ordered by reading_id then id."""
    lo, hi = sorted((int(start_reading_id), int(end_reading_id)))
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM metric_snapshots
               WHERE reading_id BETWEEN ? AND ?
               ORDER BY reading_id ASC, id ASC""",
            (lo, hi),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Influence (deltas) ─────────────────────────────────────────────────────

def _compute_influence_row(reading_id: int, metric_name: str,
                           before: dict, after: dict) -> dict:
    """Build the influence row dict for a single metric. Pure function."""
    v_before = _to_float(_extract_metric(before, metric_name))
    v_after  = _to_float(_extract_metric(after,  metric_name))

    delta = None
    pct_change = None
    if v_before is not None and v_after is not None:
        delta = v_after - v_before
        if v_before != 0:
            pct_change = (delta / v_before) * 100.0

    # A change is significant when:
    #   - it crosses the percent threshold, OR
    #   - the metric became defined / undefined between snapshots.
    significant = False
    if pct_change is not None and abs(pct_change) >= SIGNIFICANCE_PCT_THRESHOLD:
        significant = True
    elif (v_before is None) != (v_after is None):
        significant = True
    elif v_before == 0 and v_after not in (None, 0):
        # Going from "no value" to a non-zero value: meaningful, but pct math
        # blows up — flag it explicitly.
        significant = True

    return {
        "reading_id":     int(reading_id),
        "metric_name":    metric_name,
        "value_before":   v_before,
        "value_after":    v_after,
        "delta":          delta,
        "pct_change":     pct_change,
        "is_significant": 1 if significant else 0,
        "computed_at":    _now(),
    }


def record_influence(reading_id: int, before: dict, after: dict,
                     metrics: list[str] | None = None) -> list[dict]:
    """
    For each metric, compute (before, after, delta, pct_change) and store one
    metric_influence row. Returns the list of stored rows (as dicts).

    `before` and `after` are full_analysis() dicts (or any nested dict that
    _extract_metric can walk). `metrics` defaults to DEFAULT_INFLUENCE_METRICS.
    """
    metric_list = list(metrics) if metrics else list(DEFAULT_INFLUENCE_METRICS)
    if not metric_list:
        return []

    rows = [
        _compute_influence_row(reading_id, m, before or {}, after or {})
        for m in metric_list
    ]

    cols = ["reading_id", "metric_name", "value_before", "value_after",
            "delta", "pct_change", "is_significant", "computed_at"]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO metric_influence ({', '.join(cols)}) VALUES ({placeholders})"

    with _get_conn() as conn:
        conn.executemany(sql, [[r[c] for c in cols] for r in rows])

    return rows


def get_recent_influence(limit: int = 50) -> list[dict]:
    """
    Return the most-recent metric_influence rows, each joined with the keyed
    metric_snapshots row so the caller has context (verdict, captured_at,
    etc.) without a second query.

    Snapshot fields are prefixed with `snapshot_` to avoid colliding with
    influence column names. snapshot_json is omitted to keep the payload
    light — fetch get_snapshot() directly if you need the full dump.
    """
    lim = max(1, int(limit))
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                i.id              AS id,
                i.reading_id      AS reading_id,
                i.metric_name     AS metric_name,
                i.value_before    AS value_before,
                i.value_after     AS value_after,
                i.delta           AS delta,
                i.pct_change      AS pct_change,
                i.is_significant  AS is_significant,
                i.computed_at     AS computed_at,
                s.id              AS snapshot_id,
                s.captured_at     AS snapshot_captured_at,
                s.verdict         AS snapshot_verdict,
                s.verdict_score   AS snapshot_verdict_score,
                s.overall_rate    AS snapshot_overall_rate,
                s.recent_1h_rate  AS snapshot_recent_1h_rate,
                s.peak_rate       AS snapshot_peak_rate,
                s.reading_count   AS snapshot_reading_count
            FROM metric_influence i
            LEFT JOIN (
                SELECT MAX(id) AS id, reading_id
                FROM metric_snapshots
                GROUP BY reading_id
            ) latest ON latest.reading_id = i.reading_id
            LEFT JOIN metric_snapshots s ON s.id = latest.id
            ORDER BY i.id DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Source quality ─────────────────────────────────────────────────────────

def _confidence_for(source: str, reading_count: int,
                    hours_covered: float, packages_contributed: int) -> float:
    """
    Confidence is the source's base weight, scaled by a volume factor that
    saturates so a single OCR reading doesn't outrank 30 manual ones.

    Returns a value in [0, 1].
    """
    base = SOURCE_WEIGHTS.get(source, 0.5)
    # Volume factor approaches 1 around 30 readings (~1h of stream coverage).
    volume = min(1.0, (max(0, reading_count) / 30.0))
    hours_factor = min(1.0, max(0.0, (hours_covered or 0.0) / 24.0))
    # Blend: 70 % source authority, 30 % accumulated evidence.
    score = base * (0.7 + 0.2 * volume + 0.1 * hours_factor)
    return round(min(1.0, max(0.0, score)), 4)


def recompute_source_quality(readings: list[dict]) -> None:
    """
    Aggregate the current state of every source into source_quality_log.

    Idempotent: each call fully replaces the rows so the table always
    reflects "as of now". Sources absent from `readings` are removed.
    """
    aggregates: dict[str, dict] = {}
    for r in readings or []:
        src = r.get("source") or "ocr"
        agg = aggregates.setdefault(src, {
            "reading_count":        0,
            "min_hours":             None,
            "max_hours":             None,
            "packages_contributed": 0,
            "latest_reading_id":    None,
        })
        agg["reading_count"] += 1
        hours = r.get("stream_hours")
        if isinstance(hours, (int, float)):
            agg["min_hours"] = hours if agg["min_hours"] is None else min(agg["min_hours"], hours)
            agg["max_hours"] = hours if agg["max_hours"] is None else max(agg["max_hours"], hours)
        pkgs = r.get("packages")
        if isinstance(pkgs, (int, float)):
            # "packages_contributed" approximates the cumulative packages
            # reachable from this source — using the max value seen is a
            # safe proxy since `packages` is monotonic per stream.
            agg["packages_contributed"] = max(agg["packages_contributed"], int(pkgs))
        rid = r.get("id")
        if rid is not None:
            if agg["latest_reading_id"] is None or rid > agg["latest_reading_id"]:
                agg["latest_reading_id"] = int(rid)

    now = _now()
    with _get_conn() as conn:
        conn.execute("DELETE FROM source_quality_log")
        for src, agg in aggregates.items():
            hours_covered = None
            if agg["min_hours"] is not None and agg["max_hours"] is not None:
                hours_covered = round(agg["max_hours"] - agg["min_hours"], 4)
            confidence = _confidence_for(
                src, agg["reading_count"],
                hours_covered or 0.0,
                agg["packages_contributed"],
            )
            conn.execute(
                """INSERT INTO source_quality_log
                   (source, reading_count, hours_covered, packages_contributed,
                    latest_reading_id, confidence_score, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (src, agg["reading_count"], hours_covered,
                 agg["packages_contributed"], agg["latest_reading_id"],
                 confidence, now),
            )


def get_source_quality() -> list[dict]:
    """Return every source_quality_log row, highest-confidence first."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM source_quality_log
               ORDER BY confidence_score DESC, reading_count DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


# ── Module self-test entry point ───────────────────────────────────────────

if __name__ == "__main__":
    # Trivial smoke test: only initializes the schema. Real testing lives in
    # the test suite — this is a "did import + DDL run?" check.
    init_provenance_db()
    print(f"provenance.db ready at {PROVENANCE_DB_PATH}")
