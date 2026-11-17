"""Offline tests for the search API: multi-select filters, sorting, grouping.

These exercise the SQL directly against a temporary database. No server, no
network — the same reasoning as tests/test_payload.py: a broken filter should
fail here, not silently return the wrong rows in the UI.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    import server
    monkeypatch.setattr(server.db, "DB_DIR", tmp_path)
    monkeypatch.setattr(server.db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()

    rows = [
        # id, source, session, turn, origin, prompt, title, ctx, ts
        ("a1", "scout", "s1", 0, "human", "alpha deploy the service", "T1", "/proj/a", "2026-03-01T09:00:00+00:00"),
        ("a2", "scout", "s1", 1, "human", "alpha second turn longer text here", "T1", "/proj/a", "2026-03-01T09:05:00+00:00"),
        ("b1", "copilot-cli", "s2", 0, "human", "beta " + "x" * 300, "T2", "/proj/b", "2026-03-02T10:00:00+00:00"),
        ("c1", "m365-copilot", "s3", 0, "automation", "gamma you are an agent", "T3", "chat", "2026-03-03T11:00:00+00:00"),
        ("d1", "cowork", "s4", 0, "heartbeat", "delta ping", "T4", "task", "2026-03-04T12:00:00+00:00"),
    ]
    for (pid, src, sess, turn, org, prompt, title, ctx, ts) in rows:
        c.execute(
            "INSERT INTO prompts (id, source, source_session_id, turn_index, origin,"
            " prompt, title, app_context, ts_source, ts_observed, first_seen_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, src, sess, turn, org, prompt, title, ctx, ts, ts, ts, ts))
    c.commit()
    yield c
    c.close()


def run(**kw):
    import server
    kw.setdefault("q", "")
    kw.setdefault("source", None)
    kw.setdefault("context", None)
    kw.setdefault("agent", None)
    kw.setdefault("frm", None)
    kw.setdefault("to", None)
    kw.setdefault("sort", "recent")
    kw.setdefault("limit", 50)
    kw.setdefault("offset", 0)
    kw.setdefault("origin", "any")
    kw.setdefault("group", None)
    rows, total, _next, _unit = server.search(**kw)
    return rows, total


def run_full(**kw):
    """Same, but keeps next_offset and the pagination unit."""
    import server
    base = dict(q="", source=None, context=None, agent=None, frm=None, to=None,
                sort="recent", limit=50, offset=0, origin="any", group=None)
    base.update(kw)
    return server.search(**base)


# ------------------------------------------------------------ multi-select
def test_single_source_still_works(conn):
    rows, total = run(source="scout")
    assert total == 2
    assert {r["source"] for r in rows} == {"scout"}


def test_multiple_sources(conn):
    """The whole point: more than one filter value at a time."""
    rows, total = run(source="scout,copilot-cli")
    assert total == 3
    assert {r["source"] for r in rows} == {"scout", "copilot-cli"}


def test_multiple_origins(conn):
    rows, total = run(origin="automation,heartbeat")
    assert total == 2
    assert {r["origin"] for r in rows} == {"automation", "heartbeat"}


def test_origin_any_ignores_other_values(conn):
    _, total = run(origin="any")
    assert total == 5


def test_origin_nonhuman(conn):
    rows, total = run(origin="nonhuman")
    assert total == 2
    assert "human" not in {r["origin"] for r in rows}


def test_nonhuman_combined_with_explicit_origin(conn):
    """nonhuman is a shorthand for a set, so it has to OR with the rest."""
    _, total = run(origin="nonhuman,human")
    assert total == 5


def test_multi_source_and_multi_origin_together(conn):
    _, total = run(source="scout,cowork", origin="human,heartbeat")
    assert total == 3


def test_blank_and_whitespace_values_are_ignored(conn):
    _, total = run(source="scout, ,")
    assert total == 2


# ------------------------------------------------------------------ sorting
def test_sort_recent_and_oldest_are_inverses(conn):
    newest, _ = run(sort="recent")
    oldest, _ = run(sort="oldest")
    assert [r["id"] for r in newest] == list(reversed([r["id"] for r in oldest]))


def test_sort_longest(conn):
    rows, _ = run(sort="longest")
    assert rows[0]["id"] == "b1"
    lens = [len(r["prompt"]) for r in rows]
    assert lens == sorted(lens, reverse=True)


def test_sort_shortest(conn):
    rows, _ = run(sort="shortest")
    lens = [len(r["prompt"]) for r in rows]
    assert lens == sorted(lens)


def test_unknown_sort_falls_back_to_recent(conn):
    rows, _ = run(sort="nonsense-value")
    assert rows[0]["id"] == "d1"


# ----------------------------------------------------------------- grouping
def test_group_by_source_keeps_groups_contiguous(conn):
    """Grouping happens in SQL so it survives pagination.

    If it were done in the browser, a group would be split across pages and the
    header count would describe the page rather than the result set.
    """
    rows, _ = run(group="source", origin="any")
    keys = [r["grp"] for r in rows]
    assert keys == sorted(keys)
    assert len(set(keys)) == 4


def test_group_survives_pagination(conn):
    page1, _ = run(group="source", origin="any", limit=2, offset=0)
    page2, _ = run(group="source", origin="any", limit=2, offset=2)
    combined = [r["id"] for r in page1] + [r["id"] for r in page2]
    full, _ = run(group="source", origin="any", limit=4, offset=0)
    assert combined == [r["id"] for r in full]


def test_group_by_session_orders_turns_chronologically(conn):
    """A transcript out of sequence is not a useful thing to look at."""
    rows, _ = run(group="session", origin="any", sort="longest")
    s1 = [r for r in rows if r["grp"] == "s1"]
    assert [r["turn_index"] for r in s1] == [0, 1]


def test_session_grouping_pages_whole_conversations(conn):
    """Sessions page as conversations, so a thread is never split.

    Row-based paging would cut a transcript across a page boundary, and the
    window-function ordering it required measured 8.6s on the real corpus.
    """
    rows, total, next_offset, unit = run_full(group="session", origin="any", limit=2)
    assert unit == "conversations"
    assert total == 4                      # s1..s4, not 5 rows
    assert next_offset == 2                # advanced by conversations
    assert {r["grp"] for r in rows} == {"s4", "s3"}   # newest first


def test_a_multi_turn_conversation_is_never_split(conn):
    """s1 has two turns; asking for the page it lands on returns both."""
    rows, _, _, _ = run_full(group="session", origin="any", limit=2, offset=2)
    assert {r["grp"] for r in rows} == {"s2", "s1"}
    assert [r["id"] for r in rows if r["grp"] == "s1"] == ["a1", "a2"]


def test_session_pages_do_not_overlap_or_skip(conn):
    a, _, off_a, _ = run_full(group="session", origin="any", limit=2, offset=0)
    b, _, _, _ = run_full(group="session", origin="any", limit=2, offset=off_a)
    ids_a = [r["id"] for r in a]
    ids_b = [r["id"] for r in b]
    assert not set(ids_a) & set(ids_b)
    assert sorted(ids_a + ids_b) == ["a1", "a2", "b1", "c1", "d1"]


def test_session_grouping_newest_conversation_first(conn):
    rows, _ = run(group="session", origin="any")
    order = []
    for r in rows:
        if r["grp"] not in order:
            order.append(r["grp"])
    assert order == ["s4", "s3", "s2", "s1"]


def test_session_grouping_respects_filters(conn):
    _, total, _, unit = run_full(group="session", origin="human")
    assert unit == "conversations"
    assert total == 2                      # s1 and s2 only


def test_session_grouping_empty_result(conn):
    rows, total, _, unit = run_full(group="session", source="nope", origin="any")
    assert rows == [] and total == 0 and unit == "conversations"


def test_row_grouping_reports_prompt_unit(conn):
    _, _, next_offset, unit = run_full(group="source", origin="any", limit=2)
    assert unit == "prompts"
    assert next_offset == 2


def test_group_by_day(conn):
    rows, _ = run(group="day", origin="any")
    assert rows[0]["grp"] == "2026-03-04"
    keys = [r["grp"] for r in rows]
    assert keys == sorted(keys, reverse=True)


def test_group_by_context(conn):
    rows, _ = run(group="context", origin="any")
    assert set(r["grp"] for r in rows) == {"/proj/a", "/proj/b", "chat", "task"}


def test_unknown_group_is_ignored(conn):
    rows, total = run(group="not-a-group", origin="any")
    assert total == 5
    assert all(r["grp"] is None for r in rows)


def test_no_group_returns_null_key(conn):
    rows, _ = run(origin="any")
    assert all(r["grp"] is None for r in rows)


# ------------------------------------------------------------------- search
def test_search_still_filters_and_groups(conn):
    rows, total = run(q="alpha", origin="any", group="source")
    assert total == 2
    assert all("alpha" in r["prompt"] for r in rows)


def test_search_with_multi_source(conn):
    """Note fts_query quotes every token, so multi-word input is an AND.

    "alpha OR beta" would search for the literal word "OR" too. That is
    pre-existing behaviour of the query builder, not something grouping or
    multi-select changed, so this asserts the combination works with a single
    term rather than pretending boolean syntax is supported.
    """
    _, total = run(q="alpha", source="scout,copilot-cli", origin="any")
    assert total == 2
    _, none = run(q="alpha", source="cowork", origin="any")
    assert none == 0


def test_search_snippet_present(conn):
    rows, _ = run(q="alpha", origin="any")
    assert rows[0]["snip"] is not None
