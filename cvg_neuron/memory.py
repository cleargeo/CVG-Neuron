"""
CVG Neuron — Persistent Memory System
SQLite-backed memory that stores: conversations, infrastructure observations,
learned patterns, and flagged events. This is Neuron's working memory.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DATA_DIR = Path(os.environ.get("NEURON_DATA_DIR", "/data/neuron"))
DB_PATH  = DATA_DIR / "memory.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,          -- 'user' | 'assistant' | 'system'
    content     TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    context_tag TEXT                       -- optional topic tag
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT    NOT NULL,          -- 'infrastructure' | 'security' | 'deployment' | 'anomaly'
    subject     TEXT    NOT NULL,          -- service name, host, etc.
    detail      TEXT    NOT NULL,
    severity    TEXT    NOT NULL DEFAULT 'info',   -- 'info' | 'warning' | 'critical'
    resolved    INTEGER NOT NULL DEFAULT 0,
    ts          REAL    NOT NULL,
    source      TEXT                       -- which system generated this
);

CREATE TABLE IF NOT EXISTS patterns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key TEXT    NOT NULL UNIQUE,
    description TEXT    NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL,
    metadata    TEXT                       -- JSON
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT    NOT NULL,          -- 'deploy' | 'alert' | 'audit' | 'dns_change' | 'health_change'
    service     TEXT,
    payload     TEXT    NOT NULL,          -- JSON
    ts          REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_session  ON conversations (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_obs_category  ON observations  (category, resolved, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type   ON events        (event_type, ts DESC);
"""


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def store_message(session_id: str, role: str, content: str, context_tag: str = "") -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (session_id, role, content, ts, context_tag) VALUES (?,?,?,?,?)",
            (session_id, role, content, time.time(), context_tag),
        )
        return cur.lastrowid


def get_conversation(session_id: str, limit: int = 20) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT role, content, ts, context_tag FROM conversations WHERE session_id=? ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    # Return in chronological order
    return [dict(r) for r in reversed(rows)]


def list_sessions(limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("""
            SELECT session_id,
                   COUNT(*) as message_count,
                   MIN(ts)  as started,
                   MAX(ts)  as last_active,
                   MAX(CASE WHEN role='user' THEN content END) as last_user_msg
            FROM conversations
            GROUP BY session_id
            ORDER BY last_active DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Observations — infrastructure intelligence
# ---------------------------------------------------------------------------

def record_observation(
    category: str,
    subject: str,
    detail: str,
    severity: str = "info",
    source: str = "neuron",
) -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO observations (category, subject, detail, severity, ts, source) VALUES (?,?,?,?,?,?)",
            (category, subject, detail, severity, time.time(), source),
        )
        # Auto-learn pattern
        _learn_pattern_internal(conn, f"{category}:{subject}:{severity}", detail)
        return cur.lastrowid


def get_observations(
    category: str | None = None,
    severity: str | None = None,
    resolved: bool = False,
    limit: int = 100,
) -> list[dict]:
    where = ["resolved = ?"]
    params: list = [1 if resolved else 0]
    if category:
        where.append("category = ?")
        params.append(category)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    params.append(limit)

    sql = f"SELECT * FROM observations WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?"
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def resolve_observation(obs_id: int) -> bool:
    with _db() as conn:
        cur = conn.execute("UPDATE observations SET resolved=1 WHERE id=?", (obs_id,))
        return cur.rowcount > 0


def get_unresolved_warnings() -> list[dict]:
    return get_observations(severity="warning", resolved=False) + \
           get_observations(severity="critical", resolved=False)


# ---------------------------------------------------------------------------
# Pattern learning
# ---------------------------------------------------------------------------

def _learn_pattern_internal(conn: sqlite3.Connection, key: str, description: str) -> None:
    now = time.time()
    existing = conn.execute("SELECT id, occurrences FROM patterns WHERE pattern_key=?", (key,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE patterns SET occurrences=occurrences+1, last_seen=?, description=? WHERE pattern_key=?",
            (now, description, key),
        )
    else:
        conn.execute(
            "INSERT INTO patterns (pattern_key, description, occurrences, first_seen, last_seen) VALUES (?,?,1,?,?)",
            (key, description, now, now),
        )


def learn_pattern(key: str, description: str, metadata: dict | None = None) -> None:
    with _db() as conn:
        now = time.time()
        existing = conn.execute("SELECT id, occurrences FROM patterns WHERE pattern_key=?", (key,)).fetchone()
        meta_json = json.dumps(metadata) if metadata else None
        if existing:
            conn.execute(
                "UPDATE patterns SET occurrences=occurrences+1, last_seen=?, description=?, metadata=? WHERE pattern_key=?",
                (now, description, meta_json, key),
            )
        else:
            conn.execute(
                "INSERT INTO patterns (pattern_key, description, occurrences, first_seen, last_seen, metadata) VALUES (?,?,1,?,?,?)",
                (key, description, now, now, meta_json),
            )


def get_top_patterns(limit: int = 20) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM patterns ORDER BY occurrences DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def record_event(event_type: str, payload: dict, service: str = "") -> int:
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO events (event_type, service, payload, ts) VALUES (?,?,?,?)",
            (event_type, service, json.dumps(payload), time.time()),
        )
        return cur.lastrowid


def get_recent_events(event_type: str | None = None, limit: int = 50) -> list[dict]:
    if event_type:
        sql = "SELECT * FROM events WHERE event_type=? ORDER BY ts DESC LIMIT ?"
        params = (event_type, limit)
    else:
        sql = "SELECT * FROM events ORDER BY ts DESC LIMIT ?"
        params = (limit,)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            pass
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Memory summary for context injection
# ---------------------------------------------------------------------------

def build_memory_context(session_id: str | None = None) -> str:
    """Build a short text context block from recent memory for injection into prompts."""
    parts = []

    # Recent unresolved warnings
    warnings = get_unresolved_warnings()
    if warnings:
        parts.append("ACTIVE ALERTS:")
        for w in warnings[:5]:
            parts.append(f"  [{w['severity'].upper()}] {w['subject']}: {w['detail'][:120]}")

    # Recent events
    events = get_recent_events(limit=5)
    if events:
        parts.append("RECENT EVENTS:")
        for e in events:
            ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
            svc = f" ({e['service']})" if e.get("service") else ""
            parts.append(f"  {ts_str} [{e['event_type']}]{svc}")

    # Top patterns
    patterns = get_top_patterns(limit=5)
    if patterns:
        parts.append("TOP PATTERNS OBSERVED:")
        for p in patterns[:3]:
            parts.append(f"  {p['pattern_key']} (seen {p['occurrences']}x): {p['description'][:80]}")

    return "\n".join(parts) if parts else ""


def get_stats() -> dict:
    with _db() as conn:
        return {
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "sessions":      conn.execute("SELECT COUNT(DISTINCT session_id) FROM conversations").fetchone()[0],
            "observations":  conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "unresolved_warnings": conn.execute("SELECT COUNT(*) FROM observations WHERE resolved=0 AND severity IN ('warning','critical')").fetchone()[0],
            "patterns":      conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0],
            "events":        conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "db_path":       str(DB_PATH),
        }
