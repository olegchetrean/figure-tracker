"""
External-signals aggregator: pulls posts about Figure AI / F.03 / the livestream
from public sources that don't require API keys.

Sources implemented:
  - Reddit  (public JSON endpoints, no auth)
  - HackerNews (Algolia public search)

Stores in a new SQLite table `external_signals` with a UNIQUE(source, source_id)
constraint so re-polling is idempotent.

Polled every N minutes via APScheduler (started from main.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/opt/figure-tracker/data/tracker.db")
POLL_USER_AGENT = "figure-tracker/1.0 (https://github.com/figure-tracker)"
QUERY_KEYWORDS = ("figure ai", "figure 03", "figure f.03", "figure robot",
                  "humanoid robot livestream", "f.03 robot")
RELEVANCE_REGEX = re.compile(
    r"\b(figure(?:\s+ai)?|f\.?03|f03|humanoid)\b", re.IGNORECASE)


# ── DB schema ────────────────────────────────────────────────────────────────

def init_news_db() -> None:
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS external_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                posted_at TEXT,
                ingested_at TEXT NOT NULL,
                url TEXT,
                author TEXT,
                title TEXT,
                snippet TEXT,
                score INTEGER,
                relevance REAL,
                metadata TEXT,
                UNIQUE(source, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_es_source ON external_signals(source);
            CREATE INDEX IF NOT EXISTS idx_es_posted ON external_signals(posted_at);
            CREATE INDEX IF NOT EXISTS idx_es_relevance ON external_signals(relevance);
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


def _insert_signal(rec: dict) -> bool:
    try:
        with _conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO external_signals
                  (source, source_id, posted_at, ingested_at, url, author,
                   title, snippet, score, relevance, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rec["source"], rec["source_id"], rec.get("posted_at"),
                  datetime.utcnow().isoformat(), rec.get("url"),
                  rec.get("author"), rec.get("title"), rec.get("snippet"),
                  rec.get("score"), rec.get("relevance"),
                  json.dumps(rec.get("metadata") or {})))
            return c.total_changes > 0
    except sqlite3.Error as e:
        log.error("Failed to insert signal: %s", e)
        return False


def _truncate(s: str | None, n: int = 500) -> str | None:
    if not s:
        return s
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_SAFE_URL_SCHEMES = ("http://", "https://")


def _sanitise_url(url: str | None) -> str | None:
    """Reject javascript:, data:, file: and other dangerous URL schemes."""
    if not url:
        return None
    u = url.strip()
    lo = u.lower()
    if not any(lo.startswith(s) for s in _SAFE_URL_SCHEMES):
        return None
    if len(u) > 2048:
        return None
    return u


def _sanitise_text(s: str | None, max_len: int = 500) -> str | None:
    """Strip control characters and limit length — frontend also escapes."""
    if not s:
        return s
    cleaned = "".join(ch for ch in s if ch == "\n" or ord(ch) >= 32)
    return _truncate(cleaned, max_len)


def _score_relevance(text: str | None) -> float:
    """Cheap keyword-based relevance score (0–1). Saves LLM calls."""
    if not text:
        return 0.0
    text_l = text.lower()
    hits = 0
    for kw in ("figure", "f.03", "f03", "humanoid", "rose", "gary", "jim", "warehouse robot"):
        if kw in text_l:
            hits += 1
    return min(1.0, hits * 0.25)


# ── Source: Reddit ───────────────────────────────────────────────────────────

REDDIT_SUBS = ("singularity", "OpenAI", "robotics", "Figure",
               "artificial", "Futurology", "MachineLearning")
REDDIT_BASE = "https://www.reddit.com"


def poll_reddit(limit: int = 25) -> dict:
    """Poll a handful of subreddits for recent posts matching our keywords."""
    found = 0
    new = 0
    errors: list[str] = []
    with httpx.Client(timeout=15.0, headers={"User-Agent": POLL_USER_AGENT}) as cli:
        for sub in REDDIT_SUBS:
            for q in QUERY_KEYWORDS[:3]:  # first 3 keywords per sub keeps requests bounded
                url = (f"{REDDIT_BASE}/r/{sub}/search.json"
                       f"?q={httpx.QueryParams({'q': q})['q']}"
                       f"&restrict_sr=1&sort=new&limit={limit}&t=week")
                try:
                    r = cli.get(url)
                    if r.status_code != 200:
                        errors.append(f"r/{sub} '{q}': HTTP {r.status_code}")
                        continue
                    data = r.json()
                except Exception as exc:
                    errors.append(f"r/{sub} '{q}': {exc}")
                    continue

                children = data.get("data", {}).get("children", [])
                for ch in children:
                    p = ch.get("data", {})
                    title = p.get("title") or ""
                    body = p.get("selftext") or ""
                    blob = f"{title} {body}"
                    if not RELEVANCE_REGEX.search(blob):
                        continue
                    found += 1
                    rec = {
                        "source": "reddit",
                        "source_id": p.get("id"),
                        "posted_at": datetime.fromtimestamp(
                            p.get("created_utc", 0), tz=timezone.utc).isoformat(),
                        "url": _sanitise_url(f"https://reddit.com{p.get('permalink', '')}"),
                        "author": _sanitise_text(p.get("author"), 80),
                        "title": _sanitise_text(title, 300),
                        "snippet": _sanitise_text(body, 500),
                        "score": p.get("score", 0),
                        "relevance": _score_relevance(blob),
                        "metadata": {"subreddit": sub, "num_comments": p.get("num_comments", 0),
                                     "matched_query": q},
                    }
                    if _insert_signal(rec):
                        new += 1
                time.sleep(0.4)  # polite — Reddit rate-limits anonymously
    return {"source": "reddit", "found": found, "new": new, "errors": errors[:5]}


# ── Source: HackerNews ───────────────────────────────────────────────────────

HN_BASE = "https://hn.algolia.com/api/v1"


def poll_hackernews(per_query: int = 30) -> dict:
    found = 0
    new = 0
    errors: list[str] = []
    with httpx.Client(timeout=15.0) as cli:
        for q in QUERY_KEYWORDS:
            try:
                r = cli.get(f"{HN_BASE}/search_by_date",
                            params={"query": q, "tags": "story",
                                    "hitsPerPage": per_query})
                if r.status_code != 200:
                    errors.append(f"hn '{q}': HTTP {r.status_code}")
                    continue
                hits = r.json().get("hits", [])
            except Exception as exc:
                errors.append(f"hn '{q}': {exc}")
                continue

            for h in hits:
                title = h.get("title") or h.get("story_title") or ""
                if not RELEVANCE_REGEX.search(title):
                    continue
                found += 1
                rec = {
                    "source": "hackernews",
                    "source_id": str(h.get("objectID")),
                    "posted_at": h.get("created_at"),
                    "url": _sanitise_url(h.get("url"))
                           or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                    "author": _sanitise_text(h.get("author"), 80),
                    "title": _sanitise_text(title, 300),
                    "snippet": _sanitise_text(h.get("story_text"), 500),
                    "score": h.get("points", 0),
                    "relevance": _score_relevance(title),
                    "metadata": {"num_comments": h.get("num_comments", 0),
                                 "matched_query": q},
                }
                if _insert_signal(rec):
                    new += 1
            time.sleep(0.3)
    return {"source": "hackernews", "found": found, "new": new, "errors": errors[:5]}


# ── Orchestrator ─────────────────────────────────────────────────────────────

def poll_all() -> dict:
    init_news_db()
    out = {"polled_at": datetime.utcnow().isoformat(), "results": []}
    for fn in (poll_reddit, poll_hackernews):
        try:
            out["results"].append(fn())
        except Exception as exc:
            log.error("Poll failed: %s", exc, exc_info=True)
            out["results"].append({"source": fn.__name__, "error": str(exc)})
    total_new = sum(r.get("new", 0) for r in out["results"])
    out["total_new"] = total_new
    log.info("News polling: %d new signals (%s)", total_new,
             ", ".join(f"{r['source']}:{r.get('new', 0)}" for r in out["results"]))
    return out


# ── Read API ─────────────────────────────────────────────────────────────────

def get_recent_signals(limit: int = 30, min_relevance: float = 0.0) -> list[dict]:
    """Most-recent signals, optionally filtered by relevance threshold."""
    try:
        with _conn() as c:
            rows = c.execute("""
                SELECT * FROM external_signals
                WHERE relevance >= ?
                ORDER BY posted_at DESC NULLS LAST, ingested_at DESC
                LIMIT ?
            """, (min_relevance, limit)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["metadata"] = json.loads(d.get("metadata") or "{}")
                except Exception:
                    d["metadata"] = {}
                out.append(d)
            return out
    except sqlite3.Error as e:
        log.error("get_recent_signals failed: %s", e)
        return []


def signal_summary() -> dict:
    """Counts grouped by source + 24h velocity."""
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM external_signals").fetchone()[0]
            by_source = {r["source"]: r["n"] for r in c.execute(
                "SELECT source, COUNT(*) AS n FROM external_signals GROUP BY source")}
            recent_24h = c.execute("""
                SELECT COUNT(*) FROM external_signals
                WHERE posted_at >= datetime('now', '-1 day')
            """).fetchone()[0]
            avg_rel = c.execute("SELECT ROUND(AVG(relevance), 2) FROM external_signals").fetchone()[0]
        return {
            "total": total,
            "by_source": by_source,
            "last_24h": recent_24h,
            "avg_relevance": avg_rel,
        }
    except sqlite3.Error as e:
        log.error("signal_summary failed: %s", e)
        return {"total": 0, "by_source": {}, "last_24h": 0, "avg_relevance": None}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(poll_all(), indent=2))
    print("\nRecent signals:")
    for s in get_recent_signals(10):
        print(f"  [{s['source']}] {s['title']} (rel={s['relevance']})")
