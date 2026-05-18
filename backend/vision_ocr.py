"""
Vision-based frame parser using GPT-5.4-nano via the LiteLLM gateway.

Returns a structured snapshot of what's visible on the Figure AI livestream:
  - Total stream timer + cumulative package counter (always present)
  - Split-screen challenge data (when active)
  - Identity of the active robot + model (always tried, may be null)
  - Stream phase (solo_active, split_challenge, transition, ambient, unknown)

Cost: ~$0.0003 / call at high detail. At 2-minute interval that's ~$0.22/day.
"""
import base64
import json
import logging
import os
import re

import cv2
import numpy as np
from openai import OpenAI

try:
    from cost_tracker import record_call as _record_cost
except ImportError:
    _record_cost = None

log = logging.getLogger(__name__)

VISION_MODEL      = os.getenv("VISION_MODEL", "gpt-5.4-nano")
LITELLM_BASE_URL  = os.getenv("LITELLM_BASE_URL", "https://api.megapromoting.com/v1")
MAX_TOKENS        = int(os.getenv("VISION_MAX_TOKENS", "400"))
SHIFT_TOTAL_HOURS = 9.0   # The "FULL WORK SHIFT" timer on the overlay is a 9-hour countdown

VALID_PHASES = {"solo_active", "split_challenge", "transition", "ambient", "unknown"}
VALID_ERROR_TYPES = {"drop", "misplace", "pause", "jam", "grip_struggle", "recovery", "other"}
VALID_ERROR_SEVERITIES = {"low", "medium", "high"}
VALID_PACKAGE_SIZES = {"small", "medium", "large", "mixed"}
COLOR_WHITELIST = {"yellow", "red", "blue", "black", "white", "pink", "green",
                   "orange", "purple", "brown", "gray", "tan"}
MATERIAL_WHITELIST = {"plastic_bag", "cardboard_box", "padded_envelope",
                      "paper_envelope", "bubble_wrap", "soft_polybag"}

PROMPT = """You are an OCR + scene-understanding system reading a frame from the Figure AI warehouse robot YouTube livestream.

The 1280x720 frame contains these elements (read whichever are visible):

A. ALWAYS-VISIBLE OVERLAY:
   - BOTTOM-LEFT corner under label "HRS.MIN.SEC":
       a large white timer HH:MM:SS showing TOTAL STREAM TIME (e.g. "109:54:47", 2 or 3 digit hours).
   - BOTTOM-RIGHT corner under label "PACKAGES":
       a large white counter with thousands separators (e.g. "136,592").

B. SPLIT-SCREEN CHALLENGE OVERLAY (only sometimes — a rounded rectangle bar at the top-center):
   Three side-by-side fields:
     LEFT  under "INTERN" / "TOTAL PACKAGES"  → human's package count (e.g. "4,732")
     CENTER under "FULL WORK SHIFT"            → COUNTDOWN HH:MM:SS of time REMAINING in 9h shift
                                                  (so "06:18:34" means 6h 18m 34s LEFT, 2h 41m 26s ELAPSED)
     RIGHT under "F.03" / "TOTAL PACKAGES"    → robot's package count (e.g. "4,638")
   The two big numbers are USUALLY DIFFERENT — never copy the same value to both.

C. ROBOT IDENTITY (in the scene, NOT in the overlay):
   The active robot has a small white "Hello, my name is" sticker on its chest with a hand-written name
   in red marker (commonly seen names: "ROSE", "GARY", "JIM", "BOB", but ANY name is valid).
   Below that sticker is the robot MODEL label (almost always "F.03").

D. PHASE (your interpretation of the scene):
   - "split_challenge" — the top-center split-screen overlay (section B) is clearly visible
   - "solo_active"     — exactly one robot is sorting packages in the foreground, no split-screen overlay
   - "transition"      — robot handoff in progress, or robot leaving/entering, no sorting happening
   - "ambient"         — wide-angle view showing many robots / no active sorting
   - "unknown"         — none of the above clearly apply

E. SCENE COUNT: roughly how many humanoid figures (robots + humans combined) are visible in the entire frame, including background.

G. PACKAGE MIX (what's currently on the conveyor / table / in the robot's hands):
   For ANY visible packages in the frame, observe:
     - count of packages visible (rough number, including conveyor + table + held)
     - dominant colours seen (max 5): from {yellow, red, blue, black, white, pink, green, orange, purple, brown, gray, tan}
     - dominant size: "small" (envelope), "medium" (parcel), "large" (box), or "mixed"
     - materials seen (max 3): from {plastic_bag, cardboard_box, padded_envelope, paper_envelope, bubble_wrap, soft_polybag}
   Return empty lists and 0 if nothing is visible.

F. ROBOT ERROR (only flag if clearly visible in this single frame):
   Visible mistakes the robot might make include:
     - DROP: package falls off the conveyor or out of robot's hand
     - MISPLACE: package put in wrong bay / wrong side
     - PAUSE: robot frozen / not moving for the visible moment
     - JAM: conveyor or chute blocked / package stuck
     - GRIP_STRUGGLE: robot fumbling, multiple grip attempts, package slipping
     - RECOVERY: a previously dropped/stuck package being retrieved
     - OTHER: any other obvious mistake
   Only flag an error if it is CLEARLY VISIBLE in this single frame. Do not guess from context.

Reply with ONLY a JSON object (no markdown, no code fences, no prose). Keys (exactly these names, in any order):
{
  "stream_hours":    number  — total stream timer from section A, as H + M/60 + S/3600. null if unreadable.
  "packages":        integer — package counter from section A, no commas. null if unreadable.
  "is_split":        boolean — true ONLY if the top-center split-screen overlay (section B) is visibly present.
  "human_packages":  integer or null — only set when is_split=true.
  "robot_packages":  integer or null — only set when is_split=true.
  "shift_hours":     number  or null — only set when is_split=true; return the COUNTDOWN value AS DISPLAYED (e.g. "06:18:34" → 6.309). The backend converts to elapsed.
  "active_robot":    string  or null — the name visible on the foreground robot's chest sticker (uppercase, no punctuation). null if no sticker / unreadable / no foreground robot.
  "robot_model":     string  or null — the model label below the name (e.g. "F.03"). null if not visible.
  "phase":           string  — one of: "split_challenge", "solo_active", "transition", "ambient", "unknown".
  "num_figures_in_scene": integer — total humanoid figures visible (foreground + background). 0 if none.
  "error_observed": boolean — true ONLY when a robot mistake is clearly visible in this single frame.
  "error_type": one of "drop"/"misplace"/"pause"/"jam"/"grip_struggle"/"recovery"/"other", or null when error_observed is false.
  "error_severity": "low"/"medium"/"high", or null when error_observed is false.
  "error_description": short string (≤ 80 chars) describing what's visible, or null.
  "visible_packages_count": integer — packages visible in the frame (0 if none).
  "package_colors": array of strings — dominant colours (max 5), from the colour whitelist above. [] if none.
  "package_size_dominant": one of "small"/"medium"/"large"/"mixed", or null if no packages.
  "package_materials": array of strings — materials (max 3), from the material whitelist. [] if none.
}

Be precise — return null for anything unclear rather than guessing. Look at each digit of numbers carefully."""


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["LITELLM_API_KEY"],
        base_url=LITELLM_BASE_URL,
    )


def _frame_to_b64_jpg(frame: np.ndarray, quality: int = 80) -> str:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return base64.b64encode(buf).decode("ascii")


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


def parse_frame_with_vision(frame: np.ndarray) -> dict | None:
    if frame is None or frame.size == 0:
        return None

    try:
        img_b64 = _frame_to_b64_jpg(frame)
    except Exception as e:
        log.error("Frame encode failed: %s", e)
        return None

    try:
        resp = _client().chat.completions.create(
            model=VISION_MODEL,
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
        log.error("Vision API call failed: %s", e)
        return None

    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        log.warning("Vision returned empty content")
        return None

    data = _extract_json(raw)
    if not data:
        log.warning("Vision response not parseable JSON: %s", raw[:200])
        return None

    if data.get("stream_hours") is None or data.get("packages") is None:
        log.info("Vision read incomplete (stream_hours or packages null)")
        return None

    out: dict = {
        "stream_hours": float(data["stream_hours"]),
        "packages":     int(data["packages"]),
        "is_split":     bool(data.get("is_split", False)),
    }

    if out["is_split"]:
        if data.get("human_packages") is not None:
            out["human_packages"] = int(data["human_packages"])
        if data.get("robot_packages") is not None:
            out["robot_packages"] = int(data["robot_packages"])
        if data.get("shift_hours") is not None:
            remaining = float(data["shift_hours"])
            elapsed   = max(0.0, min(SHIFT_TOTAL_HOURS, SHIFT_TOTAL_HOURS - remaining))
            out["shift_hours"] = round(elapsed, 4)

    # Identity / phase fields (always tried; may be None)
    robot = data.get("active_robot")
    if robot is not None:
        robot = str(robot).strip().upper()
        out["active_robot"] = robot or None
    else:
        out["active_robot"] = None

    model = data.get("robot_model")
    if model is not None:
        out["robot_model"] = str(model).strip() or None
    else:
        out["robot_model"] = None

    phase = data.get("phase") or ("split_challenge" if out["is_split"] else "unknown")
    phase = phase.strip().lower() if isinstance(phase, str) else "unknown"
    out["phase"] = phase if phase in VALID_PHASES else "unknown"

    n_fig = data.get("num_figures_in_scene")
    out["num_figures_in_scene"] = int(n_fig) if isinstance(n_fig, (int, float)) else None

    # Robot error observation (section F) — sanitised against whitelists
    err_observed = bool(data.get("error_observed", False))
    if not err_observed:
        out["error_observed"]    = False
        out["error_type"]        = None
        out["error_severity"]    = None
        out["error_description"] = None
    else:
        out["error_observed"] = True

        etype = data.get("error_type")
        etype = etype.strip().lower() if isinstance(etype, str) else None
        out["error_type"] = etype if etype in VALID_ERROR_TYPES else "other"

        esev = data.get("error_severity")
        esev = esev.strip().lower() if isinstance(esev, str) else None
        out["error_severity"] = esev if esev in VALID_ERROR_SEVERITIES else "low"

        edesc = data.get("error_description")
        if isinstance(edesc, str):
            edesc = edesc.strip()[:120] or None
        else:
            edesc = None
        out["error_description"] = edesc

    # Package mix (section G) — sanitise + whitelist
    pkg_n = data.get("visible_packages_count")
    out["visible_packages_count"] = (int(pkg_n) if isinstance(pkg_n, (int, float))
                                     and 0 <= pkg_n <= 50 else None)

    colors_raw = data.get("package_colors") or []
    if isinstance(colors_raw, list):
        cs = []
        for c in colors_raw[:5]:
            if isinstance(c, str):
                cl = c.strip().lower()
                if cl in COLOR_WHITELIST and cl not in cs:
                    cs.append(cl)
        out["package_colors_json"] = json.dumps(cs)
    else:
        out["package_colors_json"] = "[]"

    sz = data.get("package_size_dominant")
    if isinstance(sz, str):
        sz = sz.strip().lower()
        out["package_size_dominant"] = sz if sz in VALID_PACKAGE_SIZES else None
    else:
        out["package_size_dominant"] = None

    mats_raw = data.get("package_materials") or []
    if isinstance(mats_raw, list):
        ms = []
        for m in mats_raw[:3]:
            if isinstance(m, str):
                ml = m.strip().lower()
                if ml in MATERIAL_WHITELIST and ml not in ms:
                    ms.append(ml)
        out["package_materials_json"] = json.dumps(ms)
    else:
        out["package_materials_json"] = "[]"

    try:
        u = resp.usage
        err_tag = f"err={out['error_type']}" if out["error_observed"] else "err=none"
        log.info("Vision OK [%s] tokens in=%d out=%d phase=%s robot=%s %s",
                 VISION_MODEL, u.prompt_tokens, u.completion_tokens,
                 out["phase"], out["active_robot"], err_tag)
        if _record_cost is not None:
            try:
                _record_cost(
                    model=VISION_MODEL,
                    prompt_tokens=u.prompt_tokens,
                    completion_tokens=u.completion_tokens,
                    detail="high",
                )
            except Exception as e:
                log.debug("cost record failed: %s", e)
    except Exception:
        pass

    return out
