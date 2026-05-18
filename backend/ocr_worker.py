"""
OCR Worker — captures Figure AI YouTube live stream every CAPTURE_INTERVAL
seconds, sends the frame to a vision model (gpt-5.4-nano via LiteLLM),
parses the on-screen overlay, and stores the reading in SQLite.

Replaces the previous Tesseract pipeline. The vision model is far more
robust to font changes, layout drift, and threshold tuning issues.
"""
import logging
import os
import subprocess
import tempfile
import time

import cv2
import numpy as np

from database import get_all_readings, get_latest_reading, get_latest_reading_id, init_db, insert_reading
from influence_hook import insert_with_influence
from vision_ocr import parse_frame_with_vision

# Sanity bounds — reject obviously-corrupt OCR readings.
MAX_STREAM_HOUR_JUMP = 5.0      # >5h jump between consecutive 2-min reads = OCR digit slip
MAX_STREAM_HOURS_ABS = 10_000.0  # stream isn't going to last >416 days
MAX_PACKAGES_JUMP    = 50_000    # >50k jump between reads = OCR digit slip on counter
MAX_PACKAGES_ABS     = 10_000_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OCR] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

FIGURE_LIVE_URL = os.getenv(
    "FIGURE_LIVE_URL",
    "https://www.youtube.com/watch?v=luU57hMhkak",
)
CAPTURE_INTERVAL  = int(os.getenv("CAPTURE_INTERVAL", "120"))
URL_REFRESH_EVERY = 100                            # cycles
COOKIES_FILE      = os.getenv("YT_COOKIES", "/opt/figure-tracker/youtube_cookies.txt")


# ── YouTube helpers ──────────────────────────────────────────────────────────

def get_stream_url() -> str | None:
    cmd = [
        "yt-dlp",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "-f", "best[height<=720]/best",
        "--get-url",
        "--no-playlist",
    ]
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(FIGURE_LIVE_URL)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        lines = [l for l in result.stdout.strip().splitlines() if l.startswith("http")]
        if not lines:
            tail = result.stderr.splitlines()[-1] if result.stderr else "no output"
            log.info("Stream not live (yt-dlp: %s)", tail)
            return None
        return lines[0]
    except Exception as e:
        log.error("yt-dlp failed: %s", e)
        return None


def grab_frame(stream_url: str) -> np.ndarray | None:
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        tmp = f.name
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", stream_url,
             "-frames:v", "1", "-update", "1", "-q:v", "2", tmp],
            capture_output=True, timeout=60,
        )
        frame = cv2.imread(tmp)
        if frame is not None:
            return frame
        log.warning("ffmpeg returned but frame is empty (rc=%d)", result.returncode)
    except Exception as e:
        log.error("ffmpeg grab failed: %s", e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None


# ── Sanity + duplicate guards ────────────────────────────────────────────────

def is_sane(data: dict) -> tuple[bool, str]:
    """
    Reject obviously-corrupt OCR readings before they pollute the DB.
    Returns (ok, reason).
    """
    sh = data.get("stream_hours")
    pk = data.get("packages")
    if sh is None or pk is None:
        return False, "missing stream_hours or packages"
    if sh <= 0 or sh > MAX_STREAM_HOURS_ABS:
        return False, f"stream_hours={sh} out of absolute bounds"
    if pk < 0 or pk > MAX_PACKAGES_ABS:
        return False, f"packages={pk} out of absolute bounds"

    latest = get_latest_reading()
    if not latest:
        return True, "first reading"

    # Stream timer cannot go backwards more than a tiny rounding margin
    if sh < latest["stream_hours"] - 0.01:
        return False, f"stream_hours went backwards: {latest['stream_hours']:.3f} → {sh:.3f}"
    # And cannot jump by absurd amount (digit-slip)
    if sh - latest["stream_hours"] > MAX_STREAM_HOUR_JUMP:
        return False, f"stream_hours jumped by {sh - latest['stream_hours']:.1f}h (>{MAX_STREAM_HOUR_JUMP}h limit)"

    # Packages counter cannot decrease (Figure's display only counts up)
    if pk < latest["packages"]:
        return False, f"packages went backwards: {latest['packages']:,} → {pk:,}"
    if pk - latest["packages"] > MAX_PACKAGES_JUMP:
        return False, f"packages jumped by {pk - latest['packages']:,} (>{MAX_PACKAGES_JUMP} limit)"

    return True, "ok"


def is_new(data: dict) -> bool:
    latest = get_latest_reading()
    if not latest:
        return True
    return data["packages"] != latest["packages"]


# ── Main loop ────────────────────────────────────────────────────────────────

def run():
    init_db()
    log.info("Worker starting. Interval=%ds, model=%s",
             CAPTURE_INTERVAL, os.getenv("VISION_MODEL", "gpt-5.4-nano"))

    stream_url: str | None = None
    cycle = 0

    while True:
        try:
            if stream_url is None or cycle % URL_REFRESH_EVERY == 0:
                log.info("Refreshing stream URL…")
                stream_url = get_stream_url()
                if not stream_url:
                    log.warning("Stream offline — retrying in 60 s")
                    time.sleep(60)
                    continue

            frame = grab_frame(stream_url)
            if frame is None:
                log.warning("Empty frame — resetting URL")
                stream_url = None
                time.sleep(15)
                continue

            data = parse_frame_with_vision(frame)
            if data is None:
                log.warning("Vision parse failed on this frame")
                cycle += 1; time.sleep(CAPTURE_INTERVAL); continue

            ok, reason = is_sane(data)
            if not ok:
                log.warning("REJECTED corrupt OCR reading: %s | data=%s", reason, data)
                cycle += 1; time.sleep(CAPTURE_INTERVAL); continue

            if is_new(data):
                new_reading = {
                    "stream_hours":            data["stream_hours"],
                    "packages":                data["packages"],
                    "is_split":                data.get("is_split", False),
                    "human_packages":          data.get("human_packages"),
                    "robot_packages":          data.get("robot_packages"),
                    "shift_hours":             data.get("shift_hours"),
                    "source":                  "ocr",
                    "active_robot":            data.get("active_robot"),
                    "robot_model":             data.get("robot_model"),
                    "phase":                   data.get("phase"),
                    "num_figures_in_scene":    data.get("num_figures_in_scene"),
                    "error_observed":          data.get("error_observed", False),
                    "error_type":              data.get("error_type"),
                    "error_severity":          data.get("error_severity"),
                    "error_description":       data.get("error_description"),
                    "visible_packages_count":  data.get("visible_packages_count"),
                    "package_colors_json":     data.get("package_colors_json"),
                    "package_size_dominant":   data.get("package_size_dominant"),
                    "package_materials_json":  data.get("package_materials_json"),
                }
                result = insert_with_influence(
                    new_reading=new_reading,
                    get_all_readings_fn=get_all_readings,
                    insert_reading_fn=insert_reading,
                    get_latest_reading_id_fn=get_latest_reading_id,
                )
                sig = result.get("significant_metrics") or []
                verdict_flip = " VERDICT-FLIPPED" if result.get("verdict_changed") else ""
                log.info(
                    "Saved  %07.3fh → %s pkgs%s%s%s",
                    data["stream_hours"],
                    f"{data['packages']:,}",
                    "  [SPLIT]" if data.get("is_split") else "",
                    f"  changes={','.join(sig)}" if sig else "",
                    verdict_flip,
                )
            else:
                log.debug("No change in packages count — skipping insert")

            cycle += 1
            time.sleep(CAPTURE_INTERVAL)

        except KeyboardInterrupt:
            log.info("Worker stopped by user")
            break
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
            stream_url = None
            time.sleep(60)


if __name__ == "__main__":
    run()
