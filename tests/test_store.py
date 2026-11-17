"""Tests for the store() safety guarantee.

The whole re-harvest plan rests on one invariant: running extraction a second
time over a conversation we already hold must ENRICH those rows and must never
corrupt them. `prompts.id` is sha256(source|session|turn_index), so a turn
index that no longer means the same turn would otherwise write the wrong
timestamp onto the wrong prompt — silently, since the corpus has almost no
duplication for it to show up against.

The alignment dry-run showed this is not hypothetical: on a 25-conversation
sample the DOM extractor had stored an attachment JSON blob as one prompt,
prefixed another with stray words, and missed turns in the middle of a third.

These run against a temporary database. No browser, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from collectors import Item  # noqa: E402
from harvest import _same_prompt, store  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    c = db.connect()
    yield c
    c.close()


@pytest.fixture()
def item():
    return Item(source="m365-copilot", session_id="conv-1", title="Title",
                recency_rank=3, deep_link="https://example/conv-1", app_context="chat")


def rows(conn):
    return conn.execute(
        "SELECT turn_index, prompt, response_excerpt, ts_source, first_seen_at,"
        "       message_id, author, title"
        "  FROM prompts ORDER BY turn_index").fetchall()


def dom_blocks():
    """What the old DOM extractor produced: text only."""
    return [{"prompt": "first question", "response": "first answer"},
            {"prompt": "second question", "response": "second answer"}]


def payload_blocks():
    """What the payload produces: same turns, plus metadata."""
    return [
        {"prompt": "first question", "response": "first answer",
         "ts_source": "2026-03-03T09:52:14+00:00", "author": "user",
         "message_id": "m-1", "title": "Real Title"},
        {"prompt": "second question", "response": "second answer",
         "ts_source": "2026-03-03T09:55:01+00:00", "author": "user",
         "message_id": "m-2", "title": "Real Title"},
    ]


# ------------------------------------------------------------------- basics
def test_first_pass_inserts(conn, item):
    new, enriched, conflicts = store(conn, item, dom_blocks(), "extract")
    assert (new, enriched, conflicts) == (2, 0, 0)
    assert len(rows(conn)) == 2


def test_rerun_is_idempotent_no_duplicates(conn, item):
    store(conn, item, dom_blocks(), "extract")
    store(conn, item, dom_blocks(), "extract")
    store(conn, item, dom_blocks(), "extract")
    assert len(rows(conn)) == 2


# ---------------------------------------------------------------- enrichment
def test_backfill_fills_timestamps_and_metadata(conn, item):
    store(conn, item, dom_blocks(), "extract")
    assert all(r["ts_source"] is None for r in rows(conn))

    new, enriched, conflicts = store(conn, item, payload_blocks(), "backfill")
    assert (new, conflicts) == (0, 0)
    assert enriched == 2

    got = rows(conn)
    assert [r["ts_source"] for r in got] == [
        "2026-03-03T09:52:14+00:00", "2026-03-03T09:55:01+00:00"]
    assert [r["message_id"] for r in got] == ["m-1", "m-2"]
    assert all(r["author"] == "user" for r in got)


def test_first_seen_at_is_never_rewritten(conn, item):
    """It records when WE first saw the row; later knowledge does not change it."""
    store(conn, item, dom_blocks(), "extract")
    before = [r["first_seen_at"] for r in rows(conn)]
    store(conn, item, payload_blocks(), "backfill")
    assert [r["first_seen_at"] for r in rows(conn)] == before


def test_degraded_extraction_cannot_blank_existing_data(conn, item):
    """An empty response must never overwrite a response we already hold."""
    store(conn, item, payload_blocks(), "backfill")
    store(conn, item, [{"prompt": "first question", "response": ""},
                       {"prompt": "second question", "response": ""}], "extract")
    assert all(r["response_excerpt"] for r in rows(conn))


def test_response_is_filled_when_previously_empty(conn, item):
    store(conn, item, [{"prompt": "first question", "response": ""}], "extract")
    assert rows(conn)[0]["response_excerpt"] == ""
    store(conn, item, payload_blocks(), "backfill")
    assert rows(conn)[0]["response_excerpt"] == "first answer"


# ------------------------------------------------------------ THE INVARIANT
def test_conflicting_prompt_is_skipped_not_overwritten(conn, item):
    """A turn index that no longer means the same turn must not be enriched.

    This is the case the alignment run found: the stored prompt was an
    attachment blob, the payload's was the real question. Writing the payload's
    timestamp onto that row would attach the wrong date to the wrong prompt.
    """
    store(conn, item, dom_blocks(), "extract")
    shifted = [
        {"prompt": "COMPLETELY DIFFERENT TURN", "response": "x",
         "ts_source": "2026-03-03T09:52:14+00:00", "message_id": "m-1"},
        {"prompt": "second question", "response": "second answer",
         "ts_source": "2026-03-03T09:55:01+00:00", "message_id": "m-2"},
    ]
    new, enriched, conflicts = store(conn, item, shifted, "backfill")

    assert conflicts == 1
    assert enriched == 1
    assert new == 0

    got = rows(conn)
    assert got[0]["prompt"] == "first question"      # untouched
    assert got[0]["ts_source"] is None               # no wrong date written
    assert got[1]["ts_source"] == "2026-03-03T09:55:01+00:00"


def test_extra_trailing_turns_are_appended(conn, item):
    """Turns the DOM extractor missed are genuinely new rows, not conflicts."""
    store(conn, item, dom_blocks(), "extract")
    longer = payload_blocks() + [
        {"prompt": "third question", "response": "third answer",
         "ts_source": "2026-03-03T10:00:00+00:00", "message_id": "m-3"}]
    new, enriched, conflicts = store(conn, item, longer, "backfill")
    assert (new, enriched, conflicts) == (1, 2, 0)
    assert len(rows(conn)) == 3


def test_markdown_versus_rendered_text_is_not_a_conflict(conn, item):
    """The payload carries raw markdown where the DOM carried rendered text.

    Byte comparison would call nearly every re-harvested turn a conflict and
    the backfill would do nothing at all.
    """
    store(conn, item, [{"prompt": "Deploy   the\n\nservice", "response": "ok"}], "extract")
    _, enriched, conflicts = store(
        conn, item,
        [{"prompt": "deploy the service", "response": "ok",
          "ts_source": "2026-03-03T09:52:14+00:00"}], "backfill")
    assert conflicts == 0
    assert enriched == 1


def test_overwrite_conflicts_replaces_bad_stored_text(conn, item):
    """Opt-in path: the payload is authoritative.

    Every conflict inspected on real data was the DOM extractor being wrong —
    an attachment blob stored as a prompt, a prompt truncated to one character.
    With the flag set, those rows are corrected rather than skipped.
    """
    store(conn, item, [{"prompt": '{"text":{"content":"x"},"pictures":[]}',
                        "response": ""}], "extract")
    new, enriched, conflicts = store(
        conn, item,
        [{"prompt": "what is the actual question?", "response": "the answer",
          "ts_source": "2026-03-03T09:52:14+00:00", "message_id": "m-1"}],
        "backfill", overwrite_conflicts=True)

    assert (new, conflicts) == (0, 0)
    assert enriched == 1
    got = rows(conn)[0]
    assert got["prompt"] == "what is the actual question?"
    assert got["ts_source"] == "2026-03-03T09:52:14+00:00"


def test_overwrite_conflicts_is_off_by_default(conn, item):
    """The destructive path must never be reachable by accident."""
    store(conn, item, [{"prompt": "original", "response": ""}], "extract")
    _, _, conflicts = store(conn, item, [{"prompt": "different", "response": ""}],
                            "backfill")
    assert conflicts == 1
    assert rows(conn)[0]["prompt"] == "original"


@pytest.mark.parametrize("a,b,expected", [
    ("same text", "same text", True),
    ("Same  Text", "same text", True),
    ("same\ntext", "same text", True),
    ("one thing", "another thing", False),
    ("", "", True),
])
def test_same_prompt_comparison(a, b, expected):
    assert _same_prompt(a, b) is expected


# ------------------------------------------------------------ harvest_state
def test_conversation_dates_land_in_harvest_state(conn, item):
    conn.execute(
        "INSERT INTO harvest_state (source, source_session_id, discovered_at)"
        " VALUES (?,?,?)", (item.source, item.session_id, db.now_iso()))
    blocks = payload_blocks()
    blocks[0]["created_utc"] = "2026-03-03T09:52:14+00:00"
    blocks[0]["updated_utc"] = "2026-03-03T10:05:48+00:00"
    store(conn, item, blocks, "backfill")
    row = conn.execute(
        "SELECT created_utc, updated_utc, last_harvested_at FROM harvest_state"
        " WHERE source_session_id=?", (item.session_id,)).fetchone()
    assert row["created_utc"] == "2026-03-03T09:52:14+00:00"
    assert row["updated_utc"] == "2026-03-03T10:05:48+00:00"
    assert row["last_harvested_at"]


def test_recency_rank_is_stored_from_the_item(conn, item):
    """It used to be hardcoded 0 on both write paths."""
    store(conn, item, dom_blocks(), "extract")
    assert conn.execute("SELECT DISTINCT recency_rank FROM prompts").fetchone()[0] == 3
