"""Local usage import and exact cost reporting tests.

All source logs and databases here are synthetic and contain no real prompt text.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from cost_usage import _store_call, import_local_usage  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path / "prompt-spider")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "prompt-spider" / "prompts.db")
    db.init_db()
    result = db.connect()
    yield result
    result.close()


def _insert_prompt(conn, source, session, turn, text="sanitized fixture prompt"):
    pid = db.prompt_id(source, session, turn)
    conn.execute(
        "INSERT INTO prompts (id,source,source_session_id,turn_index,ts_observed,"
        " ts_source,prompt,title,origin,first_seen_at,last_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pid, source, session, turn, "2026-11-01T10:00:00+00:00",
         "2026-11-01T10:00:00+00:00", text, "Fixture", "human",
         "2026-11-01T10:00:00+00:00", "2026-11-01T10:00:00+00:00"))
    conn.commit()
    return pid


def _event(event_id, event_type, data=None, parent=None, timestamp="2026-11-01T10:00:00Z"):
    return {"id": event_id, "type": event_type, "data": data or {},
            "parentId": parent, "timestamp": timestamp}


def _usage(event_id, call_id, parent, amount, *, model="model-a",
           initiator="user", status="complete", timestamp="2026-11-01T10:00:01Z"):
    payload = {"accounting": {"sourceSessionId": "native-session",
                              "usageId": call_id, "sequence": 1},
               "copilotUsage": {"tokenDetails": [], "totalNanoAiu": amount},
               "aiCreditsStatus": status, "model": model, "initiator": initiator,
               "inputTokens": 1, "outputTokens": 2}
    return _event(event_id, "session.usage_record", {"usage": payload},
                  parent, timestamp)


def _write_events(path: Path, events, *, tail=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(
        json.dumps(event, separators=(",", ":")).encode() + b"\n"
        for event in events) + tail)


def test_additive_migration_repeats_without_changing_prompt_ids(conn):
    pid = _insert_prompt(conn, "scout", "old-session", 2)
    before = conn.execute("SELECT id,prompt FROM prompts").fetchall()
    db._migrate(conn)
    db._migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert [tuple(r) for r in conn.execute("SELECT id,prompt FROM prompts")] == [
        tuple(r) for r in before]
    assert conn.execute("SELECT id FROM prompts").fetchone()[0] == pid
    assert {"prompt_usage_calls", "prompt_usage_revisions", "cost_import_state"} <= {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


def test_existing_database_is_backed_up_before_migration(tmp_path, monkeypatch):
    root = tmp_path / "prompt-spider"
    root.mkdir()
    path = root / "prompts.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE prompts (id TEXT PRIMARY KEY,source TEXT NOT NULL,"
        "source_session_id TEXT NOT NULL,turn_index INTEGER,ts_observed TEXT NOT NULL,"
        "ts_source TEXT,recency_rank INTEGER,prompt TEXT NOT NULL,response_excerpt TEXT,"
        "title TEXT,app_context TEXT,deep_link TEXT,content_hash TEXT,provenance TEXT,"
        "origin TEXT,message_id TEXT,author TEXT,first_seen_at TEXT,last_seen_at TEXT)")
    legacy.execute(
        "INSERT INTO prompts (id,source,source_session_id,ts_observed,prompt)"
        " VALUES ('old-id','scout','old-session','2026-01-01','sanitized')")
    legacy.execute(
        "CREATE VIRTUAL TABLE prompts_fts USING fts5(prompt,title,"
        "content='prompts',content_rowid='rowid')")
    legacy.execute(
        "INSERT INTO prompts_fts(rowid,prompt,title)"
        " SELECT rowid,prompt,title FROM prompts")
    legacy.commit()
    legacy.close()
    monkeypatch.setattr(db, "DB_DIR", root)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    backup = sqlite3.connect(root / "prompts.db.pre-v1.bak")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
    assert backup.execute("SELECT id,prompt FROM prompts").fetchone() == (
        "old-id", "sanitized")
    backup.close()
    upgraded = db.connect()
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 1
    upgraded.close()


def test_current_log_uses_event_identity_not_repeated_turn_id(conn, tmp_path):
    root = tmp_path / "events"
    first = _event("user-event-1", "user.message",
                   {"content": "sanitized first prompt", "turnId": "0",
                    "messageId": "message-1", "interactionId": "interaction-1"})
    second = _event("user-event-2", "user.message",
                    {"content": "sanitized second prompt", "turnId": "0",
                     "messageId": "message-2", "interactionId": "interaction-2"},
                    timestamp="2026-11-01T10:01:00Z")
    events = [first, _event("turn-1", "assistant.turn_start", parent="user-event-1"),
              _usage("call-event-1", "call-1", "turn-1", 22_160_565_000),
              _event("helper-1", "assistant.turn_start", parent="user-event-2"),
              _usage("call-event-2", "call-2", "helper-1", 0, model="model-b",
                     initiator="sub-agent", timestamp="2026-11-01T10:01:01Z"),
              second]
    # Put the second user event before its descendant to reflect the log's
    # append order, while keeping turnId equal across two distinct messages.
    events = [first, events[1], events[3], second, events[2], events[4]]
    path = root / "session-1" / "events.jsonl"
    _write_events(path, events, tail=b'{"id":"unfinished')

    outcome = import_local_usage(conn, current_root=root, historical_stores={})
    assert outcome[0]["status"] == "partial"
    prompts = conn.execute(
        "SELECT id,turn_index,message_id,interaction_id FROM prompts"
        " WHERE source='copilot-app' ORDER BY turn_index").fetchall()
    assert [(r["turn_index"], r["message_id"], r["interaction_id"]) for r in prompts] == [
        (0, "user-event-1", "interaction-1"),
        (1, "user-event-2", "interaction-2"),
    ]
    calls = conn.execute(
        "SELECT prompt_id,native_message_id,interaction_id,call_kind,total_nano_aiu"
        " FROM prompt_usage_calls ORDER BY native_call_id").fetchall()
    assert len(calls) == 2
    assert calls[0]["prompt_id"] == prompts[0]["id"]
    assert calls[0]["native_message_id"] == "message-1"
    assert calls[0]["interaction_id"] == "interaction-1"
    assert calls[1]["prompt_id"] == prompts[1]["id"]
    assert calls[1]["call_kind"] == "helper"
    assert calls[1]["total_nano_aiu"] == "0"


def test_replay_is_idempotent_and_corrected_amount_is_revision(conn, tmp_path):
    root = tmp_path / "events"
    path = root / "session-2" / "events.jsonl"
    owner = _event("owner", "user.message",
                   {"content": "safe prompt", "messageId": "native-message",
                    "interactionId": "interaction"})
    turn = _event("turn", "assistant.turn_start", parent="owner")
    call = _usage("event-call", "stable-usage-id", "turn", 1_234_567_890)
    _write_events(path, [owner, turn, call])
    import_local_usage(conn, current_root=root, historical_stores={})
    path.touch()
    import_local_usage(conn, current_root=root, historical_stores={})
    assert conn.execute("SELECT COUNT(*) FROM prompt_usage_calls").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM prompt_usage_revisions").fetchone()[0] == 1

    corrected = _usage("event-call", "stable-usage-id", "turn", 2_000_000_000)
    _write_events(path, [owner, turn, corrected])
    import_local_usage(conn, current_root=root, historical_stores={})
    assert conn.execute(
        "SELECT total_nano_aiu FROM prompt_usage_calls").fetchone()[0] == "2000000000"
    assert conn.execute("SELECT COUNT(*) FROM prompt_usage_calls").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM prompt_usage_revisions").fetchone()[0] == 2


def test_incomplete_amount_is_null_and_unlinked_call_stays_unlinked(conn, tmp_path):
    root = tmp_path / "events"
    path = root / "session-3" / "events.jsonl"
    _write_events(path, [_usage("no-owner", "call-no-owner", "missing", None,
                                status="incomplete")])
    import_local_usage(conn, current_root=root, historical_stores={})
    row = conn.execute(
        "SELECT prompt_id,link_status,link_reason,total_nano_aiu,credits,status"
        " FROM prompt_usage_calls").fetchone()
    assert row["prompt_id"] is None
    assert row["link_status"] == "unlinked"
    assert row["total_nano_aiu"] is None and row["credits"] is None
    assert row["status"] == "missing_amount"


def test_delayed_owner_links_on_next_import_and_checkpoint_is_not_a_call(conn, tmp_path):
    root = tmp_path / "events"
    path = root / "session-delay" / "events.jsonl"
    usage = _usage("late-call", "late-usage", "later-parent", 10)
    checkpoint = _event(
        "checkpoint", "session.usage_checkpoint",
        {"usage": {"copilotUsage": {"totalNanoAiu": 999_999_999_999}}})
    _write_events(path, [usage, checkpoint])
    import_local_usage(conn, current_root=root, historical_stores={})
    row = conn.execute(
        "SELECT prompt_id,link_reason FROM prompt_usage_calls").fetchone()
    assert row["prompt_id"] is None and row["link_reason"] == "parent_event_missing"
    assert conn.execute("SELECT COUNT(*) FROM prompt_usage_calls").fetchone()[0] == 1

    owner = _event("owner-late", "user.message",
                   {"content": "delayed safe prompt", "messageId": "late-message",
                    "interactionId": "late-interaction"})
    parent = _event("later-parent", "assistant.turn_start", parent="owner-late")
    _write_events(path, [owner, parent, usage, checkpoint])
    import_local_usage(conn, current_root=root, historical_stores={})
    row = conn.execute(
        "SELECT prompt_id,link_status FROM prompt_usage_calls").fetchone()
    assert row["prompt_id"] is not None and row["link_status"] == "linked"
    assert conn.execute("SELECT COUNT(*) FROM prompt_usage_calls").fetchone()[0] == 1


def test_historical_read_only_store_links_only_existing_prompt(conn, tmp_path):
    path = tmp_path / "historical.db"
    source = sqlite3.connect(path)
    source.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, host_type TEXT);"
        "CREATE TABLE turns (session_id TEXT, turn_index INTEGER, timestamp TEXT);"
        "CREATE TABLE assistant_usage_events (id INTEGER PRIMARY KEY, session_id TEXT,"
        " turn_index INTEGER, model TEXT, input_tokens INTEGER, output_tokens INTEGER,"
        " cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,"
        " total_nano_aiu INTEGER, initiator TEXT, token_details_json TEXT, created_at TEXT);"
        "INSERT INTO sessions VALUES ('old-session','github');"
        "INSERT INTO turns VALUES ('old-session',0,'2026-10-28T10:00:00Z');"
        "INSERT INTO turns VALUES ('old-session',1,'2026-10-28T10:01:00Z');"
        "INSERT INTO assistant_usage_events VALUES"
        " (1,'old-session',0,'model-a',4,3,2,1,0,500000000,'user','[]',"
        " '2026-10-28T10:00:01Z');"
        "INSERT INTO assistant_usage_events VALUES"
        " (2,'old-session',1,'model-b',1,1,0,0,0,700000000,'agent','[]',"
        " '2026-10-28T10:01:01Z');")
    source.commit()
    source.close()
    linked_id = _insert_prompt(conn, "copilot-cli", "old-session", 0)
    outcomes = import_local_usage(
        conn, current_root=tmp_path / "no-current",
        historical_stores={"fixture-store": path})
    assert outcomes[0]["status"] == "ok"
    rows = conn.execute(
        "SELECT prompt_id,link_status,link_reason,call_kind FROM prompt_usage_calls"
        " ORDER BY native_call_id").fetchall()
    assert rows[0]["prompt_id"] == linked_id and rows[0]["link_status"] == "linked"
    assert rows[1]["prompt_id"] is None
    assert rows[1]["link_reason"] == "prompt_not_harvested"
    assert rows[1]["call_kind"] == "helper"
    readonly = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    assert readonly.execute("SELECT COUNT(*) FROM assistant_usage_events").fetchone()[0] == 2
    readonly.close()


def test_cost_api_totals_search_csv_and_unknown_sort(conn, tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server.db, "DB_DIR", db.DB_DIR)
    monkeypatch.setattr(server.db, "DB_PATH", db.DB_PATH)
    root = tmp_path / "events"
    path = root / "session-cost" / "events.jsonl"
    owner = _event("owner", "user.message",
                   {"content": "sanitized total prompt", "messageId": "m",
                    "interactionId": "i"})
    turn = _event("turn", "assistant.turn_start", parent="owner")
    call = _usage("event-1", "call-1", "turn", 1_234_567_890)
    failed = _usage("event-failed", "call-failed", "turn", 500_000_000,
                    model="=MODEL()", initiator="sub-agent")
    failed["data"]["usage"]["finishReason"] = "error"
    zero = _usage("event-zero", "call-zero", "turn", 0)
    missing = _usage("event-2", "call-2", "turn", None, status="incomplete")
    checkpoint = _event(
        "checkpoint", "session.usage_checkpoint",
        {"usage": {"copilotUsage": {"totalNanoAiu": 999_999_999_999}}})
    _write_events(path, [owner, turn, call, failed, zero, missing, checkpoint])
    import_local_usage(conn, current_root=root, historical_stores={})

    summary = server.api_cost_summary(frm=None, to=None, source=None, model=None)
    assert summary["local_credits"] == "1.73456789"
    assert summary["local_usd_equivalent"] == "0.0173456789"
    assert summary["linked_calls"] == 4
    assert summary["missing_amount_calls"] == 1
    assert summary["unlinked_calls"] == 0
    assert sum(int(x["nano_credits"] or 0) for x in summary["by_model"]) == 1_734_567_890

    _insert_prompt(conn, "scout", "unknown-session", 0, "another safe prompt")
    rows, total, _, _ = server.search(
        "", None, None, None, None, None, "cost", 50, 0, "any", None)
    assert total == 2
    assert rows[0]["local_credits"] == "1.73456789"
    assert rows[0]["cost_status"] == "partial"
    assert server.search("", None, None, None, None, None, "recent",
                         50, 0, "any", None, "2")[1] == 0
    assert rows[1]["local_credits"] is None
    detail = server.api_cost_prompt(rows[0]["id"], limit=50, offset=0)
    assert detail["coverage"]["helper_calls"] == 1
    assert detail["cost_status"] == "partial"
    assert any(call["credits"] == "0" for call in detail["calls"])
    top = server.api_cost_top(limit=5, offset=0, frm=None, to=None, source=None, model=None)
    assert top["results"][0]["local_credits"] == "1.73456789"

    response = server.api_cost_export(frm=None, to=None, source=None,
                                      model=None, limit=50000)
    import asyncio
    async def read_response():
        return b"".join([chunk async for chunk in response.body_iterator])
    csv_text = asyncio.run(read_response()).decode("utf-8-sig")
    assert "sanitized total prompt" not in csv_text
    assert "call-1" not in csv_text
    assert "1.23456789" in csv_text
    assert "0.5" in csv_text
    assert "'=MODEL()" in csv_text


def test_exact_large_nano_sum_and_utc_bounds(conn):
    pid = _insert_prompt(conn, "scout", "large-session", 0)
    conn.execute(
        "INSERT INTO prompt_usage_calls (call_key,source_kind,source_key,prompt_source,"
        " source_session_id,native_call_id,prompt_id,link_status,link_reason,call_kind,"
        " total_nano_aiu,credits,usd_equivalent,event_at,observed_at,content_hash,"
        " revision_hash,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("large-1", "historical_store", "fixture", "scout", "large-session", "1",
         pid, "linked", None, "direct", "9007199254740993123456789",
         "9007199.254740993123456789", "90071.99254740993123456789",
         "2026-10-31T23:59:59.999999+00:00", "2026-11-01T00:00:00+00:00",
         "hash", "revision", "recorded"))
    conn.commit()
    import server
    summary = server.api_cost_summary(frm="2026-11-01", to="2026-11-02",
                                     source=None, model=None)
    assert summary["calls"] == 0
    earlier = server.api_cost_summary(frm="2026-10-31", to="2026-11-01",
                                      source=None, model=None)
    assert earlier["nano_credits"] == "9007199254740993123456789"
    assert earlier["local_credits"] == "9007199254740993.123456789"


def test_cost_sections_filter_before_limit_and_sort_amounts_numerically(conn):
    import server
    records = [
        ("Zeta target", "900000000", "model-z", "2026-10-01", "scout"),
        ("Alpha target", "1100000000", "model-a", "2026-10-02", "copilot-cli"),
        ("Other largest", "9000000000", "model-x", "2026-10-03", "scout"),
        ("Delta target", "10000000000", "model-d", "2026-10-04", "copilot-app"),
    ]
    for index, (title, nano, model, day, source) in enumerate(records):
        session = f"section-session-{index}"
        prompt_id = _insert_prompt(conn, source, session, 0, f"sanitized {title}")
        conn.execute("UPDATE prompts SET title=? WHERE id=?", (title, prompt_id))
        _store_call(
            conn, source_kind="historical_store", source_key="section-fixture",
            prompt_source=source, source_session_id=session, native_call_id=str(index),
            prompt_id=prompt_id, turn_index=0, link_status="linked", link_reason=None,
            model=model, initiator="user", nano=nano, amount_status="recorded",
            event_at=f"{day}T12:00:00+00:00", source_generation="fixture")
    conn.commit()

    top = server.api_cost_top(
        limit=1, offset=0, frm=None, to=None, source=None, model=None,
        q="target", sort="credits", direction="desc")
    assert top["total"] == 3
    assert top["results"][0]["title"] == "Delta target"
    assert top["results"][0]["known_nano"] == "10000000000"
    smallest = server.api_cost_top(
        limit=1, offset=0, frm=None, to=None, source=None, model=None,
        q="target", sort="credits", direction="asc")
    assert smallest["results"][0]["title"] == "Zeta target"
    title_order = server.api_cost_top(
        limit=1, offset=0, frm=None, to=None, source=None, model=None,
        q="target", sort="title", direction="asc")
    assert title_order["results"][0]["title"] == "Alpha target"
    no_prompt_matches = server.api_cost_top(
        limit=1, offset=0, frm=None, to=None, source=None, model=None,
        q="not found", sort="credits", direction="desc")
    assert no_prompt_matches["total"] == 0

    models = server.api_cost_breakdown(
        group="model", q="model-", sort="credits", direction="desc",
        limit=1, offset=0, frm=None, to=None, source=None, model=None)
    assert models["total"] == 4
    assert models["results"][0]["name"] == "model-d"
    assert models["results"][0]["nano_credits"] == "10000000000"
    oldest_day = server.api_cost_breakdown(
        group="day", q="2026-10-", sort="name", direction="asc",
        limit=1, offset=0, frm=None, to=None, source=None, model=None)
    assert oldest_day["results"][0]["name"] == "2026-10-01"
    source_filter = server.api_cost_breakdown(
        group="source", q="scout", sort="calls", direction="desc",
        limit=10, offset=0, frm=None, to=None, source="scout", model=None)
    assert source_filter["total"] == 1
    assert source_filter["results"][0]["name"] == "scout"
    no_group_matches = server.api_cost_breakdown(
        group="model", q="missing", sort="name", direction="asc",
        limit=10, offset=0, frm=None, to=None, source=None, model=None)
    assert no_group_matches["total"] == 0


def test_cost_prompt_ranking_finds_extremes_beyond_first_page(conn):
    import server

    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    prompts = []
    calls = []
    for index in range(60):
        prompt_id = f"rank-{index:03d}"
        source = "scout" if index % 2 == 0 else "copilot-app"
        timestamp = (start + timedelta(days=index)).isoformat()
        prompts.append((
            prompt_id, source, f"ranking-session-{index:03d}", 0,
            timestamp, timestamp, f"sanitized ranking fixture {index}",
            f"Prompt {index:03d}", "human", timestamp, timestamp,
        ))
        nano = (
            "999999999999999999999999" if index == 58
            else "1" if index == 59 else str(1000 + index)
        )
        call_count = (
            100 if index == 58 else 1 if index == 59
            else 50 if index in (48, 49) else index + 2
        )
        for call_index in range(call_count):
            calls.append((
                f"rank-call-{index:03d}-{call_index:02d}", "fixture",
                "ranking-fixture", source, f"ranking-session-{index:03d}",
                f"{index}-{call_index}", prompt_id, "linked", "direct",
                nano if call_index == 0 else "0", timestamp,
                "2026-11-01T10:00:00+00:00", "fixture-hash", "fixture-revision",
                "recorded",
            ))
    conn.executemany(
        "INSERT INTO prompts (id,source,source_session_id,turn_index,ts_observed,"
        " ts_source,prompt,title,origin,first_seen_at,last_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", prompts)
    conn.executemany(
        "INSERT INTO prompt_usage_calls (call_key,source_kind,source_key,prompt_source,"
        " source_session_id,native_call_id,prompt_id,link_status,call_kind,"
        " total_nano_aiu,event_at,observed_at,content_hash,revision_hash,status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", calls)
    conn.commit()

    def page(sort, direction, offset=0, **filters):
        params = dict(frm=None, to=None, source=None, model=None, q="")
        params.update(filters)
        return server.api_cost_top(
            limit=50, offset=offset, sort=sort, direction=direction, **params)

    for sort in ("credits", "calls"):
        high = page(sort, "desc")
        low = page(sort, "asc")
        assert high["total"] == low["total"] == 60
        assert len(high["results"]) == len(low["results"]) == 50
        assert high["results"][0]["id"] == "rank-058"
        assert low["results"][0]["id"] == "rank-059", sort
        next_page = page(sort, "asc", 50)
        assert next_page["total"] == 60
        ids = [row["id"] for row in low["results"] + next_page["results"]]
        assert len(ids) == len(set(ids)) == 60
        if sort == "calls":
            assert low["results"][-1]["id"] == "rank-048"
            assert next_page["results"][0]["id"] == "rank-049"

    highest_credit = page("credits", "desc")["results"][0]
    lowest_credit = page("credits", "asc")["results"][0]
    assert highest_credit["known_nano"] == "999999999999999999999999"
    assert lowest_credit["known_nano"] == "1"
    assert page("title", "desc")["results"][0]["id"] == "rank-059"
    assert page("date", "desc")["results"][0]["id"] == "rank-059"
    assert page("source", "asc")["results"][0]["source"] == "copilot-app"
    filtered = page("credits", "desc", q="ranking fixture", source="scout")
    assert filtered["total"] == 30
    assert all(row["source"] == "scout" for row in filtered["results"])
    assert page("credits", "desc", offset=100)["total"] == 60


def test_replacement_is_reported_without_reimporting(conn, tmp_path):
    root = tmp_path / "events"
    path = root / "session-replace" / "events.jsonl"
    owner = _event("owner", "user.message", {"content": "safe"})
    turn = _event("turn", "assistant.turn_start", parent="owner")
    _write_events(path, [owner, turn, _usage("one", "one", "turn", 1)])
    import_local_usage(conn, current_root=root, historical_stores={})
    state = conn.execute(
        "SELECT generation FROM cost_import_state"
        " WHERE source_key='copilot-session:session-replace'").fetchone()
    conn.execute(
        "UPDATE cost_import_state SET generation='other-generation'"
        " WHERE source_key='copilot-session:session-replace'")
    conn.commit()
    result = import_local_usage(conn, current_root=root, historical_stores={})
    assert result[0]["status"] == "replaced"
    assert conn.execute("SELECT COUNT(*) FROM prompt_usage_calls").fetchone()[0] == 1
    assert state["generation"]


def test_stale_source_is_not_reported_as_current(conn, tmp_path, monkeypatch):
    import server
    path = tmp_path / "events" / "session-old" / "events.jsonl"
    owner = _event("old-owner", "user.message", {"content": "safe prompt"})
    turn = _event("old-turn", "assistant.turn_start", parent="old-owner")
    _write_events(path, [owner, turn, _usage("old-call", "old-id", "old-turn", 10)])
    import_local_usage(conn, current_root=path.parents[1], historical_stores={})
    conn.execute(
        "UPDATE cost_import_state SET source_updated_at='2000-01-01T00:00:00+00:00'"
        " WHERE source_key='copilot-session:session-old'")
    conn.commit()
    costs, _, _, _ = server.search("", None, None, None, None, None, "cost", 10, 0,
                                   "any", None)
    assert costs[0]["cost_status"] == "stale"
    summary = server.api_cost_summary(frm=None, to=None, source=None, model=None)
    assert summary["source_freshness"][0]["status"] == "stale"
