"""
FastAPI backend — serves analysis API + static frontend.
Starts the OCR worker and AI analyst scheduler as background tasks.
"""
import logging
import os
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from database import get_all_readings, get_latest_ai_report, get_latest_reading, init_db
from analysis import full_analysis
from audit_api import build_audit_payload
from provenance import init_provenance_db
from cost_tracker import init_cost_db, aggregate as cost_aggregate, daily_breakdown, projection
from news_aggregator import init_news_db, poll_all as poll_news, get_recent_signals, signal_summary
from event_detector import init_events_db, poll_event, recent_events, event_summary

log = logging.getLogger(__name__)

FRONTEND_DIR = os.getenv("FRONTEND_DIR", "/opt/figure-tracker/frontend")
ADMIN_TOKEN  = os.getenv("ADMIN_TOKEN", "").strip()  # if empty, /api/history is disabled publicly

app = FastAPI(title="Figure AI Tracker", docs_url=None, redoc_url=None)

# ── Security headers + simple rate limit ──────────────────────────────────────

CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://i.ytimg.com; "
    "frame-src https://www.youtube.com https://youtube.com https://www.youtube-nocookie.com; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"]  = CSP_POLICY
        resp.headers["X-Content-Type-Options"]   = "nosniff"
        resp.headers["X-Frame-Options"]          = "SAMEORIGIN"
        resp.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"]       = "geolocation=(), microphone=(), camera=()"
        # Cache HTML aggressively short, allow JS/CSS to be cached
        if request.url.path.endswith(".html") or request.url.path == "/":
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp


# Naïve rate limit: per-IP token bucket in-memory
import collections, time as _time
_RL_BUCKETS: dict = collections.defaultdict(lambda: {"tokens": 240.0, "ts": _time.time()})
_RL_REFILL_PER_SEC = 4.0    # +4 tokens/sec → 240 req/min sustained (handles
                            # the dashboard polling 5 endpoints @ 3s + audit page)
_RL_BURST          = 240.0  # generous burst absorbs page reloads & multi-tab

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        ip = (request.client.host if request.client else "?")
        b = _RL_BUCKETS[ip]
        now = _time.time()
        b["tokens"] = min(_RL_BURST, b["tokens"] + (now - b["ts"]) * _RL_REFILL_PER_SEC)
        b["ts"] = now
        if b["tokens"] < 1.0:
            return Response("Rate limit exceeded — slow down.",
                            status_code=429,
                            headers={"Retry-After": "5",
                                     "Content-Security-Policy": CSP_POLICY})
        b["tokens"] -= 1.0
        return await call_next(request)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # public dashboard — read-only GET endpoints
    allow_methods=["GET"],
    allow_headers=["X-Admin-Token", "Content-Type"],
    allow_credentials=False, # do NOT honour cookies — token-only auth
    max_age=86400,
)


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    init_provenance_db()
    init_cost_db()
    init_news_db()
    init_events_db()

    # Seed historical data if DB is empty
    if not get_latest_reading():
        from seed_data import seed
        seed()
        log.info("Historical data seeded.")

    # Start OCR worker in background thread
    def _run_worker():
        try:
            from ocr_worker import run
            run()
        except Exception as e:
            log.error("OCR worker crashed: %s", e, exc_info=True)

    t = threading.Thread(target=_run_worker, daemon=True, name="ocr-worker")
    t.start()
    log.info("OCR worker thread started.")

    # Background jobs: hourly AI analyst + news aggregation every 12 min
    scheduler = BackgroundScheduler()
    scheduler.add_job(_run_analyst, "interval", hours=1, id="ai_analyst")
    scheduler.add_job(_poll_news, "interval", minutes=3, id="news_aggregator",
                      next_run_time=__import__("datetime").datetime.now())
    scheduler.add_job(_poll_event, "interval", minutes=10, id="event_detector",
                      next_run_time=__import__("datetime").datetime.now())
    scheduler.start()
    log.info("AI analyst + news + event scheduler started.")


def _poll_news():
    try:
        poll_news()
    except Exception as e:
        log.error("news poll error: %s", e)


def _poll_event():
    try:
        poll_event()
    except Exception as e:
        log.error("event poll error: %s", e)


def _run_analyst():
    try:
        from ai_analyst import generate_report
        generate_report()
    except Exception as e:
        log.error("AI analyst error: %s", e)


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/latest")
def api_latest():
    return get_latest_reading() or {}


# Tiny in-process cache so /api/analysis isn't recomputed on every poll.
# TTL = 2 seconds. OCR runs every 60s anyway, so 2s feels live and saves CPU.
_ANALYSIS_CACHE = {"ts": 0.0, "value": None}
_ANALYSIS_TTL   = 2.0


@app.get("/api/analysis")
def api_analysis():
    now = __import__("time").time()
    if _ANALYSIS_CACHE["value"] is not None and (now - _ANALYSIS_CACHE["ts"]) < _ANALYSIS_TTL:
        return _ANALYSIS_CACHE["value"]
    fresh = full_analysis(get_all_readings())
    _ANALYSIS_CACHE["value"] = fresh
    _ANALYSIS_CACHE["ts"]    = now
    return fresh


@app.get("/api/history")
def api_history(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    """Full reading dump — restricted to prevent scraping."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="admin token not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")
    return get_all_readings()


@app.get("/api/report")
def api_report():
    r = get_latest_ai_report()
    return r if r else {"report": "Generating first analysis…", "created_at": None}


@app.get("/api/audit")
def api_audit():
    return build_audit_payload(get_all_readings())


@app.get("/api/costs")
def api_costs():
    return {
        "today":      cost_aggregate("today"),
        "last_24h":   cost_aggregate("24h"),
        "last_7d":    cost_aggregate("7d"),
        "last_30d":   cost_aggregate("30d"),
        "all_time":   cost_aggregate("all"),
        "daily":      daily_breakdown(14),
        "projection": projection(),
    }


@app.get("/api/news")
def api_news(limit: int = 30, min_relevance: float = 0.0):
    return {
        "summary": signal_summary(),
        "signals": get_recent_signals(limit=limit, min_relevance=min_relevance),
    }


@app.get("/api/events")
def api_events(limit: int = 50, only_notable: bool = False):
    return {
        "summary": event_summary(),
        "events": recent_events(limit=limit, only_notable=only_notable),
    }


@app.get("/api/health")
def api_health():
    latest = get_latest_reading()
    return {
        "status": "ok",
        "readings": len(get_all_readings()),
        "latest_packages": latest["packages"] if latest else None,
        "latest_hours": latest["stream_hours"] if latest else None,
    }


# ── Static frontend ───────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
