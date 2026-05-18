"""
Influence hook — glue between insert_reading and the provenance subsystem.

Captures the analysis state before and after a new reading is inserted, then
saves a snapshot, records the per-metric influence of the new datapoint, and
recomputes source-quality weights based on the updated history.

The hook is intentionally fail-soft: any error inside the provenance calls is
logged and swallowed so that the main insert path is never blocked. If
`provenance.py` is missing entirely the hook degrades into a thin pass-through
that still inserts the reading but skips provenance bookkeeping.

CLI:
    python influence_hook.py backfill
        Replay every reading in chronological order to populate the
        provenance DB from existing history (one-shot).
"""
from __future__ import annotations

import logging
import sys
from typing import Callable

from analysis import full_analysis

log = logging.getLogger(__name__)
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [influence] %(levelname)s %(message)s",
    )


# ── Provenance import (graceful) ─────────────────────────────────────────────

try:
    from provenance import (  # type: ignore
        init_provenance_db,
        record_influence,
        recompute_source_quality,
        save_snapshot,
    )
    _PROVENANCE_AVAILABLE = True
except ImportError as exc:
    log.warning(
        "provenance module unavailable (%s) — influence hook will run as a "
        "no-op pass-through until provenance.py is deployed.",
        exc,
    )
    _PROVENANCE_AVAILABLE = False

    def init_provenance_db() -> None:  # type: ignore[no-redef]
        return None

    def save_snapshot(reading_id: int, analysis: dict) -> int:  # type: ignore[no-redef]
        return 0

    def record_influence(  # type: ignore[no-redef]
        reading_id: int,
        before: dict,
        after: dict,
        metrics: list[str] | None = None,
    ) -> list[dict]:
        return []

    def recompute_source_quality(readings: list[dict]) -> None:  # type: ignore[no-redef]
        return None


# ── Constants ────────────────────────────────────────────────────────────────

# Metrics whose change qualifies as "significant" when present in influence rows.
# Provenance returns rows of the shape {"metric": str, "delta": float, ...};
# here we just surface every metric name reported as changed.
_VERDICT_KEY = "verdict"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_empty_analysis(analysis: dict) -> bool:
    """True if analysis represents the no-data sentinel from full_analysis()."""
    return not analysis or analysis.get("error") == "no_data"


def _safe_call(fn, *args, **kwargs):
    """Run a provenance call, logging exceptions but never re-raising."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — intentional broad catch
        log.error("provenance call %s failed: %s", getattr(fn, "__name__", fn), exc,
                  exc_info=True)
        return None


def _verdict_key(analysis: dict) -> str | None:
    """Pull the verdict key out of an analysis dict, defensively."""
    if _is_empty_analysis(analysis):
        return None
    verdict = analysis.get("verdict") or {}
    if isinstance(verdict, dict):
        return verdict.get("verdict")
    return None


def _resolve_reading_id(
    get_all_readings_fn: Callable[[], list[dict]],
    get_latest_reading_id_fn: Callable[[], int | None] | None,
    new_reading: dict,
) -> int | float | None:
    """
    Best-effort resolution of the freshly-inserted reading's id.

    Order of preference:
      1. caller-supplied get_latest_reading_id_fn (cheapest, exact)
      2. id of last row from get_all_readings_fn
      3. stream_hours as a fallback identifier (logged as a warning)
    """
    if get_latest_reading_id_fn is not None:
        try:
            rid = get_latest_reading_id_fn()
            if rid is not None:
                return rid
        except Exception as exc:  # noqa: BLE001
            log.warning("get_latest_reading_id_fn raised: %s", exc)

    try:
        rows = get_all_readings_fn()
        if rows:
            last = rows[-1]
            if "id" in last and last["id"] is not None:
                return last["id"]
    except Exception as exc:  # noqa: BLE001
        log.warning("get_all_readings_fn raised while resolving id: %s", exc)

    fallback = new_reading.get("stream_hours")
    log.warning(
        "Could not resolve reading_id from DB; falling back to stream_hours=%s",
        fallback,
    )
    return fallback


# ── Public API ───────────────────────────────────────────────────────────────

def insert_with_influence(
    *,
    new_reading: dict,
    get_all_readings_fn: Callable[[], list[dict]],
    insert_reading_fn: Callable[..., None],
    get_latest_reading_id_fn: Callable[[], int | None] | None = None,
) -> dict:
    """
    Insert a new reading and record its influence on the global analysis.

    Args:
        new_reading: kwargs forwarded to insert_reading_fn (stream_hours,
            packages, is_split, human_packages, robot_packages, shift_hours,
            source, ...). Required at minimum: stream_hours, packages.
        get_all_readings_fn: returns the full history as list[dict], sorted by
            stream_hours ascending.
        insert_reading_fn: persists the new reading. Called as
            insert_reading_fn(**new_reading).
        get_latest_reading_id_fn: optional accessor returning the most recent
            reading's primary key. If None, we fall back to the last row from
            get_all_readings_fn().

    Returns:
        {
            "reading_id": int | float | None,
            "snapshot_id_before": int | None,
            "snapshot_id_after": int | None,
            "influence": list[dict],
            "verdict_changed": bool,
            "significant_metrics": list[str],
        }
    """
    result: dict = {
        "reading_id": None,
        "snapshot_id_before": None,
        "snapshot_id_after": None,
        "influence": [],
        "verdict_changed": False,
        "significant_metrics": [],
    }

    # 1. Pre-state ------------------------------------------------------------
    try:
        readings_before = get_all_readings_fn() or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_all_readings_fn failed before insert: %s", exc, exc_info=True)
        readings_before = []

    analysis_before = full_analysis(readings_before) if readings_before else {"error": "no_data"}

    # 2. Insert ---------------------------------------------------------------
    # Insertion is the one operation we let propagate — if the DB write fails
    # the caller deserves to know about it. Provenance failures are different.
    insert_reading_fn(**new_reading)

    # 3. Post-state -----------------------------------------------------------
    try:
        readings_after = get_all_readings_fn() or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_all_readings_fn failed after insert: %s", exc, exc_info=True)
        return result

    if not readings_after:
        log.warning("No readings visible after insert; provenance step skipped.")
        return result

    analysis_after = full_analysis(readings_after)

    reading_id = _resolve_reading_id(
        get_all_readings_fn=lambda: readings_after,
        get_latest_reading_id_fn=get_latest_reading_id_fn,
        new_reading=new_reading,
    )
    result["reading_id"] = reading_id

    # 4. Provenance -----------------------------------------------------------
    if not _PROVENANCE_AVAILABLE:
        return result

    if _is_empty_analysis(analysis_after):
        log.warning("Post-insert analysis returned no_data; skipping snapshot.")
        return result

    snap_after = _safe_call(save_snapshot, reading_id, analysis_after)
    if isinstance(snap_after, int):
        result["snapshot_id_after"] = snap_after

    # First reading ever — nothing to compare against, just record the seed.
    if _is_empty_analysis(analysis_before) or not readings_before:
        log.info("First reading detected; snapshot saved, no influence to record.")
        _safe_call(recompute_source_quality, readings_after)
        return result

    influence_rows = _safe_call(record_influence, reading_id, analysis_before, analysis_after)
    if isinstance(influence_rows, list):
        result["influence"] = influence_rows
        result["significant_metrics"] = sorted({
            row.get("metric")
            for row in influence_rows
            if isinstance(row, dict) and row.get("metric")
        })

    result["verdict_changed"] = (
        _verdict_key(analysis_before) != _verdict_key(analysis_after)
    )

    _safe_call(recompute_source_quality, readings_after)
    return result


# ── Backfill ─────────────────────────────────────────────────────────────────

def backfill_snapshots() -> dict:
    """
    Replay existing readings in chronological order to populate the provenance
    DB without re-inserting anything into the readings table.

    For each reading R_i:
        analysis_before = full_analysis(readings[:i])
        analysis_after  = full_analysis(readings[:i+1])
        snapshot(R_i, analysis_after)
        if i > 0: record_influence(R_i, analysis_before, analysis_after)
    Finally: recompute_source_quality(readings).

    Returns:
        {"snapshots_created": int, "influence_rows": int, "errors": int}
    """
    stats = {"snapshots_created": 0, "influence_rows": 0, "errors": 0}

    if not _PROVENANCE_AVAILABLE:
        log.error("provenance module is not available — backfill aborted.")
        stats["errors"] = 1
        return stats

    # Pull all readings via the database module (the canonical source).
    try:
        from database import get_all_readings, init_db
        init_db()
        readings = get_all_readings() or []
    except Exception as exc:  # noqa: BLE001
        log.error("Could not load readings for backfill: %s", exc, exc_info=True)
        stats["errors"] += 1
        return stats

    if not readings:
        log.info("No readings to backfill.")
        return stats

    _safe_call(init_provenance_db)

    log.info("Backfilling provenance for %d readings…", len(readings))

    for i, reading in enumerate(readings):
        prefix_before = readings[:i]
        prefix_after = readings[: i + 1]

        analysis_before = (
            full_analysis(prefix_before) if prefix_before else {"error": "no_data"}
        )
        analysis_after = full_analysis(prefix_after)

        if _is_empty_analysis(analysis_after):
            log.warning("Backfill: empty analysis at index %d — skipping.", i)
            stats["errors"] += 1
            continue

        reading_id = reading.get("id", reading.get("stream_hours"))

        snap = _safe_call(save_snapshot, reading_id, analysis_after)
        if snap is None:
            stats["errors"] += 1
        else:
            stats["snapshots_created"] += 1

        if i > 0 and not _is_empty_analysis(analysis_before):
            rows = _safe_call(record_influence, reading_id, analysis_before, analysis_after)
            if isinstance(rows, list):
                stats["influence_rows"] += len(rows)
            elif rows is None:
                stats["errors"] += 1

    _safe_call(recompute_source_quality, readings)

    log.info(
        "Backfill complete: %d snapshots, %d influence rows, %d errors.",
        stats["snapshots_created"], stats["influence_rows"], stats["errors"],
    )
    return stats


# ── CLI entry point ──────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "backfill":
        print("Usage: python influence_hook.py backfill", file=sys.stderr)
        return 2

    result = backfill_snapshots()
    print(
        "snapshots_created={snapshots_created} "
        "influence_rows={influence_rows} "
        "errors={errors}".format(**result)
    )
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
