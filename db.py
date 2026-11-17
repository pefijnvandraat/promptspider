"""Prompt Spider — storage layer.

DB lives outside OneDrive: it contains customer + MIP-labelled prompt content
and must never sync to the cloud.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = Path(os.environ.get("PROMPT_SPIDER_HOME") or (Path.home() / ".prompt-spider"))
DB_PATH = DB_DIR / "prompts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
  id                TEXT PRIMARY KEY,
  source            TEXT NOT NULL,
  source_session_id TEXT NOT NULL,
  agent_id          TEXT,
  turn_index        INTEGER,
  ts_observed       TEXT NOT NULL,
  ts_source         TEXT,
  recency_rank      INTEGER,
  prompt            TEXT NOT NULL,
  response_excerpt  TEXT,
  title             TEXT,
  app_context       TEXT,
  deep_link         TEXT,
  content_hash      TEXT,
  provenance        TEXT,
  origin            TEXT,
  message_id        TEXT,
  author            TEXT,
  first_seen_at     TEXT,
  last_seen_at      TEXT
);

CREATE TABLE IF NOT EXISTS harvest_state (
  source            TEXT NOT NULL,
  source_session_id TEXT NOT NULL,
  title             TEXT,
  agent_id          TEXT,
  recency_rank      INTEGER,
  content_hash      TEXT,
  created_utc       TEXT,
  updated_utc       TEXT,
  discovered_at     TEXT,
  last_harvested_at TEXT,
  PRIMARY KEY (source, source_session_id)
);

CREATE TABLE IF NOT EXISTS harvest_runs (
  run_id       TEXT PRIMARY KEY,
  started_at   TEXT,
  ended_at     TEXT,
  status       TEXT,
  mode         TEXT,
  enumerated   INTEGER DEFAULT 0,
  visited      INTEGER DEFAULT 0,
  items_new    INTEGER DEFAULT 0,
  empty_ratio  REAL,
  error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_prompts_source  ON prompts(source);
CREATE INDEX IF NOT EXISTS idx_prompts_seen    ON prompts(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(source_session_id);
CREATE INDEX IF NOT EXISTS idx_prompts_origin  ON prompts(origin);
-- The UI's default view is newest-first over a source+origin slice. The sort
-- key must be indexed as the same COALESCE expression the query uses, and the
-- WHERE clause must reference `origin` directly (not wrapped in COALESCE), or
-- SQLite falls back to a temp B-tree over the whole table: 1.8s vs 0.00s.
CREATE INDEX IF NOT EXISTS idx_prompts_browse
  ON prompts(source, origin, COALESCE(ts_source, first_seen_at) DESC);
CREATE INDEX IF NOT EXISTS idx_prompts_browse_all
  ON prompts(origin, COALESCE(ts_source, first_seen_at) DESC);
-- Grouping by source orders by source first and the timestamp second, which
-- idx_prompts_browse cannot serve: its leading column is source, so a query
-- filtered on origin has to sort the whole slice in a temp B-tree (~960ms).
-- Leading with origin turns that into an index scan.
CREATE INDEX IF NOT EXISTS idx_prompts_group_source
  ON prompts(origin, source, COALESCE(ts_source, first_seen_at) DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts
  USING fts5(prompt, title, content='prompts', content_rowid='rowid');
"""

# FTS triggers are created separately: they reference the table, so they must
# run after both prompts and prompts_fts exist.
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS prompts_ai AFTER INSERT ON prompts BEGIN
  INSERT INTO prompts_fts(rowid, prompt, title) VALUES (new.rowid, new.prompt, new.title);
END;
CREATE TRIGGER IF NOT EXISTS prompts_ad AFTER DELETE ON prompts BEGIN
  INSERT INTO prompts_fts(prompts_fts, rowid, prompt, title)
    VALUES('delete', old.rowid, old.prompt, old.title);
END;
CREATE TRIGGER IF NOT EXISTS prompts_au AFTER UPDATE ON prompts BEGIN
  INSERT INTO prompts_fts(prompts_fts, rowid, prompt, title)
    VALUES('delete', old.rowid, old.prompt, old.title);
  INSERT INTO prompts_fts(rowid, prompt, title) VALUES (new.rowid, new.prompt, new.title);
END;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_aggregate("SUM_NANO_TEXT", 1, _NanoSum)
    conn.create_collation("DECIMAL_TEXT", _compare_decimal_text)
    conn.create_function("NANO_AT_LEAST", 2, _nano_at_least)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class _NanoSum:
    def __init__(self):
        self.total = 0
        self.seen = False

    def step(self, value):
        if value is not None:
            self.total += int(value)
            self.seen = True

    def finalize(self):
        return str(self.total) if self.seen else None


def _compare_decimal_text(left: str, right: str) -> int:
    a, b = Decimal(left), Decimal(right)
    return (a > b) - (a < b)


def _nano_at_least(nano: str | None, credits: str) -> int:
    if nano is None:
        return 0
    amount = Decimal(credits)
    sign, digits, exponent = amount.as_tuple()
    coefficient = int("".join(str(d) for d in digits) or "0")
    if sign:
        return 0
    power = exponent + 9
    if power >= 0:
        threshold = coefficient * (10 ** power)
        return int(int(nano) >= threshold)
    divisor = 10 ** -power
    return int(int(nano) * divisor >= coefficient)


def init_db() -> None:
    _backup_before_cost_migration()
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(TRIGGERS)
        _migrate(conn)


def _backup_before_cost_migration() -> None:
    """Keep one consistent copy before upgrading an existing version-0 store."""
    if not DB_PATH.exists():
        return
    source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    try:
        version = source.execute("PRAGMA user_version").fetchone()[0]
        has_prompts = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompts'"
        ).fetchone()
    finally:
        source.close()
    backup = DB_PATH.with_name("prompts.db.pre-v1.bak")
    if version >= 1 or not has_prompts or backup.exists():
        return
    temporary = backup.with_name(f"{backup.name}.{os.getpid()}.tmp")
    try:
        source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.replace(temporary, backup)
    finally:
        temporary.unlink(missing_ok=True)


def _migrate(conn) -> None:
    """Additive migrations. Safe to run on every start."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _add_columns(conn, "prompts", {
            "origin": "TEXT",
            # Platform message identity is metadata, not the content-addressed
            # primary key, so existing prompt IDs remain unchanged.
            "message_id": "TEXT",
            "author": "TEXT",
            "interaction_id": "TEXT",
        })
        _add_columns(conn, "harvest_state", {
            "created_utc": "TEXT",
            "updated_utc": "TEXT",
        })
        conn.execute("UPDATE prompts SET origin='human' WHERE origin IS NULL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_usage_calls (
              call_key TEXT PRIMARY KEY,
              source_kind TEXT NOT NULL,
              source_key TEXT NOT NULL,
              prompt_source TEXT,
              source_session_id TEXT,
              native_call_id TEXT NOT NULL,
              prompt_id TEXT,
              turn_index INTEGER,
              link_status TEXT NOT NULL,
              link_reason TEXT,
              call_kind TEXT NOT NULL,
              model TEXT,
              initiator TEXT,
              total_nano_aiu TEXT,
              credits TEXT,
              usd_equivalent TEXT,
              input_tokens INTEGER,
              output_tokens INTEGER,
              cache_read_tokens INTEGER,
              cache_write_tokens INTEGER,
              reasoning_tokens INTEGER,
              token_details_json TEXT,
              native_message_id TEXT,
              interaction_id TEXT,
              parent_event_id TEXT,
              agent_id TEXT,
              parent_tool_call_id TEXT,
              event_at TEXT,
              observed_at TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              revision_hash TEXT NOT NULL,
              source_generation TEXT,
              status TEXT NOT NULL DEFAULT 'recorded'
            )""")
        _add_columns(conn, "prompt_usage_calls", {
            "prompt_source": "TEXT",
            "parent_event_id": "TEXT",
        })
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_usage_revisions (
              call_key TEXT NOT NULL,
              revision_hash TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              total_nano_aiu TEXT,
              observed_at TEXT NOT NULL,
              PRIMARY KEY(call_key, revision_hash)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_import_state (
              source_key TEXT PRIMARY KEY,
              source_kind TEXT NOT NULL,
              status TEXT NOT NULL,
              detail TEXT,
              generation TEXT,
              byte_offset INTEGER NOT NULL DEFAULT 0,
              records_seen INTEGER NOT NULL DEFAULT 0,
              linked_calls INTEGER NOT NULL DEFAULT 0,
              unlinked_calls INTEGER NOT NULL DEFAULT 0,
              last_attempt_at TEXT NOT NULL,
              last_success_at TEXT,
              source_updated_at TEXT
            )""")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_prompt_time
              ON prompt_usage_calls(prompt_id, event_at)""")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_source
              ON prompt_usage_calls(source_kind, source_key, status)""")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # Refresh planner stats only when they are missing or clearly stale. ANALYZE
    # on a 25k-row table with several indexes takes tens of seconds, which is far
    # too slow to pay on every process start.
    try:
        has_stats = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'"
        ).fetchone()
        if not has_stats:
            conn.execute("ANALYZE")
            conn.commit()
    except Exception:
        pass


def _add_columns(conn, table: str, columns: dict[str, str]) -> None:
    """ALTER TABLE ADD COLUMN for anything missing. Idempotent."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def prompt_id(source: str, session_id: str, turn_index: int) -> str:
    return hashlib.sha256(f"{source}|{session_id}|{turn_index}".encode()).hexdigest()[:32]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]
