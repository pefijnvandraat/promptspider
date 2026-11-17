"""Prompt Spider — M365 Copilot hydration-payload parser.

Pure functions over a string. No browser, no network, no database, no lock —
so this module is unit-testable offline, which the DOM extractor never was.
That is the point: a UI change should fail a test, not silently write two
thousand empty rows at 03:30.

WHY THIS EXISTS
---------------
`collectors._EXTRACT` reads the *rendered* DOM. The rendered DOM carries no
timestamps, which is why PLAN-HANDOFF.md recorded "no source chronology" as an
accepted limitation. That is true of the DOM and false of the page that
produces it: the server ships a hydration payload containing every message with
`author`, `createdAt` and full `text`. It simply never reaches the screen.

STRUCTURE (verified against the live surface, 2026-08)
------------------------------------------------------
    <script nonce="...">window.__… = JSON.parse("…escaped json…")</script>

The literal is double-encoded: decode the JS string, then parse the JSON.

    loaderData
      chat-history.store.rawConversationResponse
        messages[]   {text, author, createdAt, timestamp, messageId, messageType,
                      contentType, turnState, requestId, storageMessageId, …}
        chatName, conversationId, createTimeUtc, updateTimeUtc, turnState
      root.store.conversationPageHistoryList.chats[]
        {conversationId, threadId, chatName, createTimeUtc, updateTimeUtc,
         isScheduledPromptThread, isUnread, turnState, …}

Both are located by SHAPE, not by path. Paths are framework routing details and
will move; "a dict holding a messages list whose entries have an author" is a
far more stable contract. Never anchor on the script `nonce` — it rotates on
every single response.

TURN GROUPING
-------------
`messages` is flat: one `user` entry followed by one or more `bot` entries. The
DOM extractor emitted one row per *user* message, so `turn_index` here counts
user messages only. That is deliberate — `prompts.id` is
sha256(source|session|turn_index), so any other indexing would produce new ids
on re-harvest and silently duplicate every conversation instead of enriching it.
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timezone

# Intermediate "thinking" turns the UI shows while composing an answer. They are
# real messages but not part of the response, and including them made stored
# excerpts noisy.
NOISE_MESSAGE_TYPES = {"Progress"}
NOISE_CONTENT_TYPES = {"SearchResults"}

_JSON_PARSE = re.compile(r"JSON\.parse\(")


def iso_utc(value) -> str | None:
    """Normalise to UTC ISO with seconds precision.

    The payload mixes two formats, which is easy to miss: messages carry ISO
    strings (`createdAt`: "2026-03-03T09:52:14.5473247+00:00") while thread and
    conversation metadata carry epoch MILLISECONDS (`createTimeUtc`:
    1772531534547). Handling only the first silently discards every
    conversation-level date.

    Sentinel zeros (`expiryTimeUtc: 0`) mean "unset" and must not become
    1970-01-01. Output matches local_sources._iso so ts_source stays comparable
    across sources and the UI's COALESCE(ts_source, first_seen_at) sort holds.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        seconds = value / 1000 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return (datetime.fromisoformat(value.replace("Z", "+00:00"))
                .astimezone(timezone.utc).isoformat(timespec="seconds"))
    except Exception:
        return value


def clean_title(value) -> str | None:
    """Tidy `chatName` for display.

    Copilot derives the title from the opening prompt, so it arrives HTML-escaped
    and can carry newlines — "Rol &amp; Doel\\nJe bent een PowerPoint-expert…"
    renders badly in a list. Unescape, collapse whitespace, and cap it.
    """
    if not isinstance(value, str):
        return None
    text = " ".join(_html.unescape(value).split())
    return text[:200] or None


def _js_string_at(text: str, start: int) -> str | None:
    """Read the JS string literal beginning at `start` (a quote character)."""
    quote = text[start]
    i = start + 1
    esc = False
    while i < len(text):
        c = text[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == quote:
            return text[start:i + 1]
        i += 1
    return None


def decode_payloads(html: str) -> list:
    """Every JSON.parse("…") argument in the document, decoded twice.

    Tolerant by design: a blob we cannot decode is skipped, not raised, so one
    malformed script block never costs us the conversation.
    """
    out = []
    for m in _JSON_PARSE.finditer(html or ""):
        j = m.end()
        while j < len(html) and html[j] in " \t\r\n":
            j += 1
        if j >= len(html) or html[j] not in "\"'":
            continue
        literal = _js_string_at(html, j)
        if not literal:
            continue
        try:
            if literal[0] == '"':
                inner = json.loads(literal)
            else:                                   # single-quoted variant
                inner = json.loads('"' + literal[1:-1].replace('"', '\\"') + '"')
            out.append(json.loads(inner))
        except Exception:
            continue
    return out


def _looks_like_conversation(node) -> bool:
    msgs = node.get("messages") if isinstance(node, dict) else None
    return (isinstance(msgs, list) and bool(msgs) and isinstance(msgs[0], dict)
            and any(k in msgs[0] for k in ("text", "author", "createdAt")))


def find_conversation(node, _depth: int = 0):
    """First dict holding a message list that looks like a transcript."""
    if _depth > 12:
        return None
    if isinstance(node, dict):
        if _looks_like_conversation(node):
            return node
        for v in node.values():
            hit = find_conversation(v, _depth + 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = find_conversation(v, _depth + 1)
            if hit is not None:
                return hit
    return None


def find_threads(node, _depth: int = 0) -> list:
    """First list of thread descriptors (sidebar metadata), if present."""
    if _depth > 12:
        return []
    if isinstance(node, dict):
        for v in node.values():
            if (isinstance(v, list) and v and isinstance(v[0], dict)
                    and "createTimeUtc" in v[0] and "conversationId" in v[0]):
                return v
            hit = find_threads(v, _depth + 1)
            if hit:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = find_threads(v, _depth + 1)
            if hit:
                return hit
    return []


def _is_noise(msg: dict) -> bool:
    return (msg.get("messageType") in NOISE_MESSAGE_TYPES
            or msg.get("contentType") in NOISE_CONTENT_TYPES)


def _card_text(node, out: list[str] | None = None, _depth: int = 0) -> list[str]:
    """Collect text out of an Adaptive Card body, in document order."""
    if out is None:
        out = []
    if _depth > 10:
        return out
    if isinstance(node, dict):
        value = node.get("text")
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        for key, child in node.items():
            if key != "text":
                _card_text(child, out, _depth + 1)
    elif isinstance(node, list):
        for child in node:
            _card_text(child, out, _depth + 1)
    return out


def message_text(msg: dict) -> str:
    """The visible text of a message, wherever the platform decided to put it.

    Assistant answers are usually delivered as Adaptive Cards, with the prose at
    `adaptiveCards[0].body[0].text` and the top-level `text` field absent
    entirely. Reading only `text` therefore captured the prompt but lost the
    answer for most conversations — every row got a timestamp and an empty
    response_excerpt. Some messages do populate `text` directly, so prefer it
    when present and fall back to the card body.
    """
    value = msg.get("text")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "\n\n".join(_card_text(msg.get("adaptiveCards")))


def group_turns(messages: list) -> list[dict]:
    """Flat message list -> one entry per user turn, responses attached.

    Bot messages arriving before any user message are ignored: they belong to
    no turn and attaching them would shift every subsequent turn_index.
    """
    turns: list[dict] = []
    current: dict | None = None
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        text = message_text(msg)
        if msg.get("author") == "user":
            current = {
                "turn_index": len(turns),
                "prompt": text,
                "response": "",
                "ts_source": iso_utc(msg.get("createdAt") or msg.get("timestamp")),
                "author": "user",
                "message_id": msg.get("messageId"),
                "_parts": [],
            }
            turns.append(current)
        elif current is not None:
            if _is_noise(msg) or not text:
                continue
            current["_parts"].append(text)

    for t in turns:
        t["response"] = "\n\n".join(t.pop("_parts"))
    return [t for t in turns if t["prompt"]]


def parse_hydration(html: str) -> dict:
    """Parse a /chat/conversation/{guid} document.

    Returns {"conversation": {...}, "turns": [...], "threads": [...]}.
    An empty `turns` list means "found nothing" — callers should treat that as
    a signal to fall back to the DOM extractor, never as a clean empty result.
    """
    result = {"conversation": {}, "turns": [], "threads": []}
    for doc in decode_payloads(html):
        if not result["turns"]:
            conv = find_conversation(doc)
            if conv:
                result["conversation"] = {
                    "conversation_id": conv.get("conversationId"),
                    "title": clean_title(conv.get("chatName")),
                    "created_utc": iso_utc(conv.get("createTimeUtc")),
                    "updated_utc": iso_utc(conv.get("updateTimeUtc")),
                    "turn_state": conv.get("turnState"),
                }
                result["turns"] = group_turns(conv.get("messages"))
        if not result["threads"]:
            result["threads"] = [
                {
                    "conversation_id": t.get("conversationId"),
                    "thread_id": t.get("threadId"),
                    "title": clean_title(t.get("chatName")),
                    "created_utc": iso_utc(t.get("createTimeUtc")),
                    "updated_utc": iso_utc(t.get("updateTimeUtc")),
                    "is_scheduled": bool(t.get("isScheduledPromptThread")),
                    "is_unread": bool(t.get("isUnread")),
                }
                for t in find_threads(doc) if isinstance(t, dict)
            ]
    return result
