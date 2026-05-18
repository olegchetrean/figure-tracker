import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "/opt/figure-tracker/data/tracker.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                stream_hours REAL NOT NULL,
                packages INTEGER NOT NULL,
                is_split INTEGER DEFAULT 0,
                human_packages INTEGER,
                robot_packages INTEGER,
                shift_hours REAL,
                source TEXT DEFAULT 'ocr',
                active_robot TEXT,
                robot_model TEXT,
                phase TEXT,
                num_figures_in_scene INTEGER,
                error_observed INTEGER DEFAULT 0,
                error_type TEXT,
                error_severity TEXT,
                error_description TEXT,
                visible_packages_count INTEGER,
                package_colors_json TEXT,
                package_size_dominant TEXT,
                package_materials_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_hours
                ON readings(ROUND(stream_hours, 3));
            CREATE INDEX IF NOT EXISTS idx_stream_hours ON readings(stream_hours);
            CREATE INDEX IF NOT EXISTS idx_active_robot ON readings(active_robot);
            CREATE INDEX IF NOT EXISTS idx_phase ON readings(phase);

            CREATE TABLE IF NOT EXISTS ai_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                report TEXT NOT NULL
            );
        """)
        # Best-effort migration: add columns if they were absent on an older schema
        for col, ddl in [
            ("active_robot",            "TEXT"),
            ("robot_model",             "TEXT"),
            ("phase",                   "TEXT"),
            ("num_figures_in_scene",    "INTEGER"),
            ("error_observed",          "INTEGER DEFAULT 0"),
            ("error_type",              "TEXT"),
            ("error_severity",          "TEXT"),
            ("error_description",       "TEXT"),
            ("visible_packages_count",  "INTEGER"),
            ("package_colors_json",     "TEXT"),
            ("package_size_dominant",   "TEXT"),
            ("package_materials_json",  "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # column already exists


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_reading(stream_hours, packages, is_split=False,
                   human_packages=None, robot_packages=None,
                   shift_hours=None, source="ocr",
                   active_robot=None, robot_model=None,
                   phase=None, num_figures_in_scene=None,
                   error_observed=False, error_type=None,
                   error_severity=None, error_description=None,
                   visible_packages_count=None, package_colors_json=None,
                   package_size_dominant=None, package_materials_json=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO readings
               (captured_at, stream_hours, packages, is_split,
                human_packages, robot_packages, shift_hours, source,
                active_robot, robot_model, phase, num_figures_in_scene,
                error_observed, error_type, error_severity, error_description,
                visible_packages_count, package_colors_json,
                package_size_dominant, package_materials_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.utcnow().isoformat(), stream_hours, packages,
             int(is_split), human_packages, robot_packages, shift_hours, source,
             active_robot, robot_model, phase, num_figures_in_scene,
             int(bool(error_observed)), error_type, error_severity, error_description,
             visible_packages_count, package_colors_json,
             package_size_dominant, package_materials_json),
        )


def get_all_readings():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM readings ORDER BY stream_hours ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_reading():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM readings ORDER BY stream_hours DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_latest_reading_id() -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(id) FROM readings").fetchone()
        return row[0] if row and row[0] is not None else None


def insert_ai_report(report):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ai_reports (created_at, report) VALUES (?, ?)",
            (datetime.utcnow().isoformat(), report),
        )


def get_latest_ai_report():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ai_reports ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
