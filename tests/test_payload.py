"""Offline tests for the hydration-payload parser.

No browser, no network, no auth, no run lock, no database. This is the point of
splitting `payload.py` out: the DOM extractor could only ever be tested against
the live surface, so a UI change surfaced as thousands of empty rows the next
morning. Here it surfaces as a failing test.

The fixture is a real capture with message text, titles, GUIDs and location
data replaced; structure, key names, enum values and timestamps are genuine.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from payload import (  # noqa: E402
    clean_title, decode_payloads, find_conversation, find_threads, group_turns,
    iso_utc, message_text, parse_hydration,
)

FIXTURE = Path(__file__).parent / "fixtures" / "conversation_payload.json"


@pytest.fixture(scope="module")
def fx():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def html(fx):
    """Rebuild the real document shape: double-encoded JSON in a script tag."""
    doc = {
        "loaderData": {
            "chat-history": {"store": {"rawConversationResponse": fx["container"]}},
            "root": {"store": {"conversationPageHistoryList": {"chats": fx["threads"]}}},
        },
        "actionData": None,
        "errors": None,
    }
    literal = json.dumps(json.dumps(doc))
    return f'<!doctype html><html><body><script nonce="kt9p9gllV3dQ==">' \
           f'window.__reactRouterContext = JSON.parse({literal})</script></body></html>'


# ----------------------------------------------------------------- decoding
def test_decode_payloads_roundtrip(html):
    docs = decode_payloads(html)
    assert len(docs) == 1
    assert "loaderData" in docs[0]


def test_nonce_is_not_an_anchor(html):
    """The nonce rotates on every response; parsing must not depend on it."""
    other = html.replace("kt9p9gllV3dQ==", "COMPLETELY-DIFFERENT-NONCE")
    assert decode_payloads(other)[0] == decode_payloads(html)[0]


def test_malformed_blob_is_skipped_not_raised(html):
    broken = html.replace("<script", '<script>window.x = JSON.parse("{not json")</script><script', 1)
    docs = decode_payloads(broken)
    assert any("loaderData" in d for d in docs if isinstance(d, dict))


@pytest.mark.parametrize("bad", ["", "<html></html>", "JSON.parse(", "JSON.parse(notaquote)"])
def test_garbage_input_returns_empty(bad):
    assert decode_payloads(bad) == []
    assert parse_hydration(bad)["turns"] == []


# ------------------------------------------------------------- shape lookup
def test_conversation_found_by_shape_not_path(fx):
    """Framework paths move; the shape contract is what we rely on."""
    buried = {"a": {"b": [{"c": {"deeply": {"nested": fx["container"]}}}]}}
    assert find_conversation(buried) is not None


def test_threads_found_by_shape(fx):
    buried = {"x": {"y": {"chats": fx["threads"]}}}
    assert len(find_threads(buried)) == len(fx["threads"])


# --------------------------------------------------------------- turn logic
def test_turn_index_counts_user_messages_only(fx):
    """The alignment invariant.

    prompts.id is sha256(source|session|turn_index). The DOM extractor emitted
    one row per USER message. If this counted all messages instead, every
    re-harvested conversation would get fresh ids and duplicate rather than
    enrich — and dedupe covers under 1% of the corpus, so nothing would catch it.
    """
    msgs = fx["container"]["messages"]
    assert [m["author"] for m in msgs] == ["user", "bot", "bot"]
    turns = group_turns(msgs)
    assert len(turns) == 1
    assert turns[0]["turn_index"] == 0


def test_response_excludes_progress_messages(fx):
    """The 'Progress'/'SearchResults' turn is UI chatter, not the answer."""
    turns = group_turns(fx["container"]["messages"])
    assert "len=1435" in turns[0]["response"]
    assert "len=52" not in turns[0]["response"]


def test_prompt_and_response_both_present(fx):
    turns = group_turns(fx["container"]["messages"])
    assert turns[0]["prompt"]
    assert turns[0]["response"]


def test_message_id_captured(fx):
    turns = group_turns(fx["container"]["messages"])
    assert turns[0]["message_id"]


def test_leading_bot_messages_do_not_shift_index(fx):
    """A bot message before any user turn belongs to no turn."""
    msgs = fx["container"]["messages"]
    turns = group_turns([{"author": "bot", "text": "orphan"}] + msgs)
    assert len(turns) == 1
    assert turns[0]["turn_index"] == 0


def test_multiple_turns_index_sequentially(fx):
    msgs = fx["container"]["messages"]
    turns = group_turns(msgs + msgs)
    assert [t["turn_index"] for t in turns] == [0, 1]


def test_empty_prompts_are_dropped():
    turns = group_turns([{"author": "user", "text": "   "},
                         {"author": "bot", "text": "answer"}])
    assert turns == []


# --------------------------------------------------------------- timestamps
def test_message_timestamp_normalised(fx):
    turns = group_turns(fx["container"]["messages"])
    assert turns[0]["ts_source"] == "2026-03-03T09:52:14+00:00"


# ------------------------------------------------------- adaptive card text
ADAPTIVE_BOT = {
    "author": "bot",
    "createdAt": "2026-03-02T11:42:10.1234567+00:00",
    "messageId": "bot-1",
    "adaptiveCards": [{"type": "AdaptiveCard",
                       "body": [{"type": "TextBlock", "text": "the real answer"}]}],
}


def test_answer_is_read_from_the_adaptive_card():
    """Assistant answers usually arrive as a card, with no top-level `text`.

    Reading only `text` captured every prompt and lost almost every response —
    rows landed with a timestamp and an empty response_excerpt.
    """
    assert message_text(ADAPTIVE_BOT) == "the real answer"


def test_top_level_text_wins_when_present():
    msg = dict(ADAPTIVE_BOT, text="direct text")
    assert message_text(msg) == "direct text"


def test_nested_card_containers_are_flattened():
    msg = {"author": "bot", "adaptiveCards": [{"body": [
        {"type": "TextBlock", "text": "first"},
        {"type": "Container", "items": [{"type": "TextBlock", "text": "second"}]},
    ]}]}
    assert message_text(msg) == "first\n\nsecond"


def test_message_with_no_text_anywhere_is_empty():
    assert message_text({"author": "bot", "adaptiveCards": []}) == ""
    assert message_text({"author": "bot"}) == ""


def test_card_backed_answer_becomes_the_turn_response():
    turns = group_turns([
        {"author": "user", "text": "a question",
         "createdAt": "2026-03-02T11:41:42+00:00", "messageId": "u-1"},
        ADAPTIVE_BOT,
    ])
    assert len(turns) == 1
    assert turns[0]["response"] == "the real answer"


def test_iso_string_input():
    assert iso_utc("2026-03-03T09:52:14.5473247+00:00") == "2026-03-03T09:52:14+00:00"
    assert iso_utc("2026-03-03T09:52:14Z") == "2026-03-03T09:52:14+00:00"


def test_epoch_milliseconds_input():
    """Thread metadata uses epoch ms, not ISO — handling only ISO loses it all."""
    assert iso_utc(1772531534547) == "2026-03-03T09:52:14+00:00"


def test_zero_epoch_is_unset_not_1970():
    """expiryTimeUtc: 0 means 'no expiry', not 1st January 1970."""
    assert iso_utc(0) is None
    assert iso_utc(-1) is None


@pytest.mark.parametrize("bad", [None, "", True, False, [], {}])
def test_iso_utc_rejects_non_dates(bad):
    assert iso_utc(bad) is None


# ------------------------------------------------------------------- titles
@pytest.mark.parametrize("raw,expected", [
    ("Rol &amp; Doel\nJe bent een expert", "Rol & Doel Je bent een expert"),
    ("  spaced   out  ", "spaced out"),
    ("&lt;tag&gt; &quot;quoted&quot;", '<tag> "quoted"'),
    ("plain title", "plain title"),
])
def test_clean_title_unescapes_and_collapses(raw, expected):
    """chatName is derived from the opening prompt, so it arrives escaped."""
    assert clean_title(raw) == expected


@pytest.mark.parametrize("bad", [None, "", "   ", 123, [], {}])
def test_clean_title_rejects_non_titles(bad):
    assert clean_title(bad) is None


def test_clean_title_is_capped():
    assert len(clean_title("x" * 500)) == 200


# ------------------------------------------------------------ full pipeline
def test_parse_hydration_end_to_end(html):
    out = parse_hydration(html)
    assert len(out["turns"]) == 1
    assert out["turns"][0]["ts_source"] == "2026-03-03T09:52:14+00:00"
    assert out["conversation"]["conversation_id"]
    assert out["conversation"]["created_utc"] == "2026-03-03T09:52:14+00:00"


def test_parse_hydration_reads_thread_metadata(html, fx):
    """Sidebar metadata dates every conversation without opening it."""
    threads = parse_hydration(html)["threads"]
    assert len(threads) == len(fx["threads"]) == 19
    assert all(t["conversation_id"] for t in threads)
    assert any(t["created_utc"] for t in threads)


def test_scheduled_flag_exposed(html):
    """isScheduledPromptThread replaces a heuristic that can misfile a human."""
    threads = parse_hydration(html)["threads"]
    assert any(t["is_scheduled"] for t in threads)
    assert all(isinstance(t["is_scheduled"], bool) for t in threads)


def test_empty_turns_signals_fallback(html):
    """No turns must mean 'use the DOM extractor', never 'clean empty result'.

    Note we break the SHAPE here (the `messages` key), not the path. Renaming
    the path leaves the conversation perfectly findable — see
    test_conversation_found_by_shape_not_path — which is exactly the resilience
    we want, and why this test has to reach for something structural.
    """
    stripped = html.replace("messages", "msgsxx")
    assert parse_hydration(stripped)["turns"] == []


def test_path_rename_alone_does_not_break_parsing(html):
    """Framework routing keys can move between deploys without costing us data."""
    moved = html.replace("rawConversationResponse", "someOtherRouteKey")
    assert len(parse_hydration(moved)["turns"]) == 1
