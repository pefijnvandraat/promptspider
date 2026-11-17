"""Local, read-only AI usage import and verified prompt attribution."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import db
from local_sources import _HOST_SOURCE, _snapshot, classify, strip_injected

HOME = Path.home()
CURRENT_ROOT = HOME / ".copilot" / "session-state"
HISTORICAL_STORES = {
    "scout-store": HOME / ".scout" / "copilot" / "session-store.db",
    "copilot-store": HOME / ".copilot" / "session-store.db",
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (TypeError, ValueError):
        return None


def _amount(value) -> tuple[str | None, str]:
    if value is None:
        return None, "missing_amount"
    if isinstance(value, bool):
        return None, "invalid_amount"
    try:
        amount = int(value)
        if str(value).strip() != str(amount) or amount < 0:
            return None, "invalid_amount"
    except (TypeError, ValueError):
        return None, "invalid_amount"
    return str(amount), "recorded"


def _credits_text(nano: str) -> str:
    whole, fractional = divmod(int(nano), 1_000_000_000)
    fraction = f"{fractional:09d}".rstrip("0")
    return f"{whole}.{fraction}" if fraction else str(whole)


def _usd_text(credits: str) -> str:
    whole, dot, fractional = credits.partition(".")
    digits = whole + fractional
    places = len(fractional) + 2
    digits = digits.rjust(places + 1, "0")
    value = digits[:-places] + "." + digits[-places:]
    value = value.rstrip("0").rstrip(".")
    return value or "0"


def _call_kind(initiator: str | None, agent_id: str | None,
               parent_tool_call_id: str | None) -> str:
    role = (initiator or "").casefold().replace("_", "-")
    if agent_id or parent_tool_call_id or role in {
        "agent", "sub-agent", "subagent", "compaction", "tool",
    }:
        return "helper"
    if role in {"user", "human"}:
        return "direct"
    return "unknown"


def _nonnegative_int(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
        return result if result >= 0 and str(value).strip() == str(result) else None
    except (TypeError, ValueError):
        return None


def _json_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state(conn, source_key: str):
    return conn.execute(
        "SELECT * FROM cost_import_state WHERE source_key=?", (source_key,)
    ).fetchone()


def _write_state(conn, *, source_key: str, source_kind: str, status: str,
                 detail: str | None, generation: str | None,
                 byte_offset: int = 0, records_seen: int = 0,
                 linked_calls: int = 0, unlinked_calls: int = 0,
                 last_success_at: str | None = None,
                 source_updated_at: str | None = None):
    conn.execute(
        "INSERT INTO cost_import_state (source_key, source_kind, status, detail,"
        " generation, byte_offset, records_seen, linked_calls, unlinked_calls,"
        " last_attempt_at, last_success_at, source_updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source_key) DO UPDATE SET source_kind=excluded.source_kind,"
        " status=excluded.status, detail=excluded.detail, generation=excluded.generation,"
        " byte_offset=excluded.byte_offset, records_seen=excluded.records_seen,"
        " linked_calls=excluded.linked_calls, unlinked_calls=excluded.unlinked_calls,"
        " last_attempt_at=excluded.last_attempt_at,"
        " last_success_at=COALESCE(excluded.last_success_at,cost_import_state.last_success_at),"
        " source_updated_at=excluded.source_updated_at",
        (source_key, source_kind, status, detail, generation, byte_offset,
         records_seen, linked_calls, unlinked_calls, _now(), last_success_at,
         source_updated_at))


def _record_revision(conn, call_key: str, revision_hash: str,
                     content_hash: str, nano: str | None):
    conn.execute(
        "INSERT OR IGNORE INTO prompt_usage_revisions"
        " (call_key, revision_hash, content_hash, total_nano_aiu, observed_at)"
        " VALUES (?,?,?,?,?)",
        (call_key, revision_hash, content_hash, nano, _now()))


def _store_call(conn, *, source_kind: str, source_key: str,
                prompt_source: str | None, source_session_id: str | None,
                native_call_id: str, prompt_id: str | None,
                turn_index: int | None, link_status: str, link_reason: str | None,
                model: str | None, initiator: str | None, nano: str | None,
                amount_status: str, input_tokens=None, output_tokens=None,
                cache_read_tokens=None, cache_write_tokens=None,
                reasoning_tokens=None, token_details=None, event_at=None,
                native_message_id=None, interaction_id=None, parent_event_id=None,
                agent_id=None, parent_tool_call_id=None, source_generation=None,
                record_status: str | None = None) -> bool:
    identity_source = (source_key if source_kind == "historical_store"
                       else source_session_id or "")
    call_key = _hash(
        f"{source_kind}|{identity_source}|{source_session_id or ''}|{native_call_id}")
    details = _json_text(token_details)
    credits = _credits_text(nano) if nano is not None else None
    usd = _usd_text(credits) if credits is not None else None
    revision_payload = {
        "nano": nano, "model": model, "initiator": initiator,
        "input": input_tokens, "output": output_tokens,
        "cache_read": cache_read_tokens, "cache_write": cache_write_tokens,
        "reasoning": reasoning_tokens, "details": details,
        "event_at": event_at, "amount_status": amount_status,
    }
    revision_hash = _hash(json.dumps(revision_payload, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False))
    content_hash = _hash(json.dumps({
        **revision_payload, "native_call_id": native_call_id,
        "source_session_id": source_session_id,
        "native_message_id": native_message_id, "interaction_id": interaction_id,
        "parent_event_id": parent_event_id,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    _record_revision(conn, call_key, revision_hash, content_hash, nano)
    call_status = record_status or amount_status
    changed = conn.execute(
        "SELECT content_hash FROM prompt_usage_calls WHERE call_key=?", (call_key,)
    ).fetchone()
    conn.execute(
        "INSERT INTO prompt_usage_calls (call_key, source_kind, source_key, prompt_source,"
        " source_session_id, native_call_id, prompt_id, turn_index, link_status, link_reason,"
        " call_kind, model, initiator, total_nano_aiu, credits, usd_equivalent,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        " reasoning_tokens, token_details_json, native_message_id, interaction_id,"
        " parent_event_id, agent_id, parent_tool_call_id, event_at, observed_at,"
        " content_hash, revision_hash, source_generation, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(call_key) DO UPDATE SET"
        " source_key=excluded.source_key, prompt_source=excluded.prompt_source,"
        " source_session_id=excluded.source_session_id, prompt_id=excluded.prompt_id,"
        " turn_index=excluded.turn_index, link_status=excluded.link_status,"
        " link_reason=excluded.link_reason, call_kind=excluded.call_kind,"
        " model=excluded.model, initiator=excluded.initiator,"
        " total_nano_aiu=excluded.total_nano_aiu, credits=excluded.credits,"
        " usd_equivalent=excluded.usd_equivalent, input_tokens=excluded.input_tokens,"
        " output_tokens=excluded.output_tokens,"
        " cache_read_tokens=excluded.cache_read_tokens,"
        " cache_write_tokens=excluded.cache_write_tokens,"
        " reasoning_tokens=excluded.reasoning_tokens,"
        " token_details_json=excluded.token_details_json,"
        " native_message_id=excluded.native_message_id,"
        " interaction_id=excluded.interaction_id, parent_event_id=excluded.parent_event_id,"
        " agent_id=excluded.agent_id, parent_tool_call_id=excluded.parent_tool_call_id,"
        " event_at=excluded.event_at, observed_at=excluded.observed_at,"
        " content_hash=excluded.content_hash, revision_hash=excluded.revision_hash,"
        " source_generation=excluded.source_generation, status=excluded.status",
        (call_key, source_kind, source_key, prompt_source, source_session_id,
         native_call_id, prompt_id, turn_index, link_status, link_reason,
         _call_kind(initiator, agent_id, parent_tool_call_id), model, initiator,
         nano, credits, usd, input_tokens, output_tokens, cache_read_tokens,
         cache_write_tokens, reasoning_tokens, details, native_message_id,
         interaction_id, parent_event_id, agent_id, parent_tool_call_id,
         event_at, _now(), content_hash, revision_hash, source_generation,
         call_status))
    return changed is None


def _source_generation(path: Path) -> str:
    stat = path.stat()
    birth = getattr(stat, "st_birthtime_ns", stat.st_ctime_ns)
    return f"{stat.st_dev}:{stat.st_ino}:{birth}"


def _source_is_replaced(conn, source_key: str, generation: str,
                        source_kind: str) -> bool:
    previous = _state(conn, source_key)
    if previous and previous["generation"] and previous["generation"] != generation:
        _write_state(
            conn, source_key=source_key, source_kind=source_kind, status="replaced",
            detail="The source file was replaced. Saved usage is kept; new records need review.",
            generation=generation, byte_offset=previous["byte_offset"],
            records_seen=previous["records_seen"], linked_calls=previous["linked_calls"],
            unlinked_calls=previous["unlinked_calls"],
            source_updated_at=previous["source_updated_at"])
        conn.commit()
        return True
    return bool(previous and previous["status"] == "replaced")


def _stored_prompt(conn, source: str, session_id: str,
                   turn_index: int | None) -> tuple[str | None, str | None]:
    if turn_index is None:
        return None, "no_source_turn"
    pid = db.prompt_id(source, session_id, turn_index)
    row = conn.execute(
        "SELECT source, source_session_id, turn_index FROM prompts WHERE id=?", (pid,)
    ).fetchone()
    if not row:
        return None, "prompt_not_harvested"
    if (row["source"], row["source_session_id"], row["turn_index"]) != (
            source, session_id, turn_index):
        return None, "prompt_identity_mismatch"
    return pid, None


def _import_historical_store(conn, source_key: str, path: Path) -> dict:
    if not path.exists():
        _write_state(conn, source_key=source_key, source_kind="historical_store",
                     status="unavailable", detail="Source file is not present.",
                     generation=None)
        conn.commit()
        return {"source": source_key, "status": "unavailable", "calls": 0}
    try:
        generation = _source_generation(path)
    except Exception as exc:
        detail = f"{type(exc).__name__}: source metadata could not be read."
        _write_state(conn, source_key=source_key, source_kind="historical_store",
                     status="error", detail=detail, generation=None)
        conn.commit()
        return {"source": source_key, "status": "error", "calls": 0, "detail": detail}
    if _source_is_replaced(conn, source_key, generation, "historical_store"):
        return {"source": source_key, "status": "replaced", "calls": 0}
    snap = None
    added = linked = unlinked = 0
    try:
        snap = _snapshot(path)
        source = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"assistant_usage_events", "sessions", "turns"} <= tables:
                raise ValueError("Required usage, session or turn table is missing.")
            usage_cols = {r[1] for r in source.execute(
                "PRAGMA table_info(assistant_usage_events)")}
            session_cols = {r[1] for r in source.execute("PRAGMA table_info(sessions)")}
            turn_cols = {r[1] for r in source.execute("PRAGMA table_info(turns)")}
            required = {"id", "session_id", "total_nano_aiu"}
            if not required <= usage_cols:
                raise ValueError("Usage table does not have the required identity and amount fields.")
            if not {"id"} <= session_cols or not {"session_id"} <= turn_cols:
                raise ValueError("Session or turn identity fields are unsupported.")
            optional_usage = {
                "turn_index", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                "initiator", "agent_id", "parent_tool_call_id",
                "token_details_json", "created_at",
            }
            select = ["u.id", "u.session_id", "u.total_nano_aiu"]
            select += [f"u.{name}" if name in usage_cols else f"NULL AS {name}"
                       for name in sorted(optional_usage)]
            select += [
                "s.host_type" if "host_type" in session_cols else "NULL AS host_type",
                "t.turn_index AS matched_turn" if "turn_index" in turn_cols
                else "NULL AS matched_turn",
                "t.timestamp AS turn_timestamp" if "timestamp" in turn_cols
                else "NULL AS turn_timestamp",
            ]
            join_turn = ("LEFT JOIN turns t ON t.session_id=u.session_id"
                         " AND t.turn_index=u.turn_index"
                         if "turn_index" in usage_cols and "turn_index" in turn_cols
                         else "LEFT JOIN turns t ON 0")
            rows = source.execute(
                "SELECT " + ", ".join(select) +
                " FROM assistant_usage_events u"
                " LEFT JOIN sessions s ON s.id=u.session_id " + join_turn)
            for row in rows:
                turn_index = row["turn_index"]
                mapped_source = _HOST_SOURCE.get(row["host_type"] or "", "scout")
                prompt_id, reason = _stored_prompt(
                    conn, mapped_source, str(row["session_id"]), turn_index
                    if row["matched_turn"] is not None else None)
                nano, amount_status = _amount(row["total_nano_aiu"])
                if amount_status == "invalid_amount":
                    prompt_id, reason = None, "invalid_recorded_amount"
                call_id = str(row["id"])
                added += _store_call(
                    conn, source_kind="historical_store", source_key=source_key,
                    prompt_source=mapped_source,
                    source_session_id=str(row["session_id"]),
                    native_call_id=call_id, prompt_id=prompt_id,
                    turn_index=turn_index if row["matched_turn"] is not None else None,
                    link_status="linked" if prompt_id else "unlinked",
                    link_reason=reason,
                    model=row["model"], initiator=row["initiator"], nano=nano,
                    amount_status=amount_status,
                    input_tokens=_nonnegative_int(row["input_tokens"]),
                    output_tokens=_nonnegative_int(row["output_tokens"]),
                    cache_read_tokens=_nonnegative_int(row["cache_read_tokens"]),
                    cache_write_tokens=_nonnegative_int(row["cache_write_tokens"]),
                    reasoning_tokens=_nonnegative_int(row["reasoning_tokens"]),
                    token_details=row["token_details_json"],
                    event_at=_iso(row["created_at"] or row["turn_timestamp"]),
                    agent_id=row["agent_id"],
                    parent_tool_call_id=row["parent_tool_call_id"],
                    source_generation=generation)
                if prompt_id:
                    linked += 1
                else:
                    unlinked += 1
            conn.commit()
        finally:
            source.close()
        now = _now()
        _write_state(
            conn, source_key=source_key, source_kind="historical_store",
            status="ok", detail=None, generation=generation,
            records_seen=added, linked_calls=linked, unlinked_calls=unlinked,
            last_success_at=now,
            source_updated_at=datetime.fromtimestamp(path.stat().st_mtime,
                                                     timezone.utc).isoformat())
        conn.commit()
        return {"source": source_key, "status": "ok", "calls": added,
                "linked": linked, "unlinked": unlinked}
    except Exception as exc:
        detail = f"{type(exc).__name__}: source read or format check failed."
        _write_state(conn, source_key=source_key, source_kind="historical_store",
                     status="error", detail=detail, generation=generation)
        conn.commit()
        return {"source": source_key, "status": "error", "calls": 0,
                "detail": detail}
    finally:
        if snap:
            snap.unlink(missing_ok=True)


def _content_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    if isinstance(value, list):
        return "\n".join(_content_text(item) for item in value
                         if _content_text(item))
    return ""


def _event_map(events: list[dict]):
    by_id: dict[str, dict] = {}
    duplicates: set[str] = set()
    for event in events:
        event_id = event.get("id")
        if event_id is None:
            continue
        key = str(event_id)
        if key in by_id:
            duplicates.add(key)
        else:
            by_id[key] = event
    return by_id, duplicates


def _owner_event(event: dict, by_id: dict[str, dict],
                 duplicates: set[str]) -> tuple[dict | None, str | None]:
    parent = event.get("parentId")
    visited: set[str] = set()
    owners: dict[str, dict] = {}
    while parent is not None:
        key = str(parent)
        if key in visited:
            return None, "parent_cycle"
        if key in duplicates:
            return None, "ambiguous_parent_event"
        visited.add(key)
        parent_event = by_id.get(key)
        if parent_event is None:
            return None, "parent_event_missing"
        if parent_event.get("type") == "user.message":
            owners[key] = parent_event
        parent = parent_event.get("parentId")
    if len(owners) == 1:
        return next(iter(owners.values())), None
    if len(owners) > 1:
        return None, "multiple_user_ancestors"
    return None, "no_user_ancestor"


def _store_current_prompt(conn, session_id: str, turn_index: int,
                          event: dict) -> str | None:
    data = event.get("data") or {}
    prompt = strip_injected(_content_text(data.get("content")))
    if not prompt:
        return None
    event_id = str(event.get("id") or "")
    if not event_id:
        return None
    source = "copilot-app"
    existing = conn.execute(
        "SELECT id, turn_index FROM prompts WHERE source=? AND source_session_id=?"
        " AND message_id=?", (source, session_id, event_id)).fetchone()
    now = _now()
    if existing:
        conn.execute("UPDATE prompts SET last_seen_at=? WHERE id=?", (now, existing["id"]))
        return existing["id"]
    pid = db.prompt_id(source, session_id, turn_index)
    if conn.execute("SELECT 1 FROM prompts WHERE id=?", (pid,)).fetchone():
        return None
    conn.execute(
        "INSERT INTO prompts (id, source, source_session_id, turn_index, ts_observed,"
        " ts_source, prompt, title, content_hash, provenance, origin, message_id,"
        " interaction_id, author, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, source, session_id, turn_index, now, _iso(event.get("timestamp")),
         prompt, "", db.content_hash(prompt), "copilot-events", classify(prompt),
         event_id, (data.get("interactionId") or None), "user", now, now))
    return pid


def _import_current_log(conn, path: Path) -> dict:
    session_id = path.parent.name
    source_key = f"copilot-session:{session_id}"
    try:
        stat = path.stat()
        generation = _source_generation(path)
        previous = _state(conn, source_key)
        if _source_is_replaced(conn, source_key, generation, "current_log"):
            return {"source": source_key, "status": "replaced", "calls": 0}
        if (previous and previous["byte_offset"] == stat.st_size
                and previous["source_updated_at"] == str(stat.st_mtime_ns)
                and previous["status"] == "ok"):
            return {"source": source_key, "status": "ok", "calls": 0, "skipped": True}
        payload = path.read_bytes()
        records = payload.splitlines(keepends=True)
        complete: list[bytes] = []
        incomplete_tail = False
        for index, line in enumerate(records):
            if line.endswith((b"\n", b"\r")):
                complete.append(line)
            elif index == len(records) - 1:
                incomplete_tail = True
        complete_bytes = sum(len(line) for line in complete)
        events: list[dict] = []
        malformed = 0
        for raw in complete:
            try:
                event = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                malformed += 1
                continue
            if isinstance(event, dict):
                events.append(event)
        by_id, duplicates = _event_map(events)
        user_events = [event for event in events if event.get("type") == "user.message"]
        event_prompts: dict[str, str] = {}
        for turn_index, event in enumerate(user_events):
            event_id = str(event.get("id") or "")
            pid = _store_current_prompt(conn, session_id, turn_index, event)
            if event_id and pid:
                event_prompts[event_id] = pid
        conn.commit()

        calls = linked = unlinked = missing_identity = 0
        newest_event = None
        for event in events:
            if event.get("type") != "session.usage_record":
                continue
            data = event.get("data") or {}
            usage = data.get("usage") or {}
            accounting = usage.get("accounting") or {}
            copilot = usage.get("copilotUsage") or {}
            native_id = accounting.get("usageId")
            if not native_id:
                missing_identity += 1
                continue
            owner, reason = _owner_event(event, by_id, duplicates)
            owner_id = str(owner.get("id")) if owner and owner.get("id") else None
            prompt_id = event_prompts.get(owner_id) if owner_id else None
            if owner and not prompt_id:
                reason = "owner_prompt_not_saved"
            is_linked = bool(prompt_id)
            nano, amount_status = _amount(copilot.get("totalNanoAiu"))
            ai_status = str(usage.get("aiCreditsStatus") or "").casefold()
            record_status = ("complete" if nano is not None and ai_status == "complete"
                             else "partial" if nano is not None
                             else amount_status)
            event_at = _iso(event.get("timestamp"))
            if event_at and (newest_event is None or event_at > newest_event):
                newest_event = event_at
            owner_data = owner.get("data") or {} if owner else {}
            source_session = str(accounting.get("sourceSessionId") or session_id)
            calls += 1
            _store_call(
                conn, source_kind="current_log", source_key=session_id,
                prompt_source="copilot-app", source_session_id=source_session,
                native_call_id=str(native_id), prompt_id=prompt_id,
                turn_index=None, link_status="linked" if is_linked else "unlinked",
                link_reason=None if is_linked else (reason or "no_verified_owner"),
                model=usage.get("model"), initiator=usage.get("initiator"),
                nano=nano, amount_status=amount_status,
                input_tokens=_nonnegative_int(usage.get("inputTokens")),
                output_tokens=_nonnegative_int(usage.get("outputTokens")),
                cache_read_tokens=_nonnegative_int(usage.get("cacheReadTokens")),
                cache_write_tokens=_nonnegative_int(usage.get("cacheWriteTokens")),
                reasoning_tokens=_nonnegative_int(usage.get("reasoningTokens")),
                token_details=copilot.get("tokenDetails"), event_at=event_at,
                native_message_id=(owner_data.get("messageId") if owner else None)
                or owner_id,
                interaction_id=owner_data.get("interactionId") if owner else None,
                parent_event_id=event.get("parentId"),
                parent_tool_call_id=usage.get("parentToolCallId"),
                source_generation=generation, record_status=record_status)
            if is_linked:
                linked += 1
            else:
                unlinked += 1
        conn.commit()
        warnings = []
        if incomplete_tail:
            warnings.append("Incomplete final line will be retried.")
        if malformed:
            warnings.append(f"{malformed} complete line(s) were malformed.")
        if missing_identity:
            warnings.append(f"{missing_identity} usage record(s) had no stable usage ID.")
        if previous and stat.st_size < previous["byte_offset"]:
            warnings.append("The log was truncated; earlier saved usage was retained.")
        detail = " ".join(warnings) or None
        status = "partial" if warnings else "ok"
        _write_state(
            conn, source_key=source_key, source_kind="current_log", status=status,
            detail=detail, generation=generation, byte_offset=complete_bytes,
            records_seen=calls, linked_calls=linked, unlinked_calls=unlinked,
            last_success_at=_now(), source_updated_at=str(stat.st_mtime_ns))
        conn.commit()
        return {"source": source_key, "status": status, "calls": calls,
                "linked": linked, "unlinked": unlinked, "detail": detail,
                "newest_event_at": newest_event}
    except Exception as exc:
        previous = _state(conn, source_key)
        detail = f"{type(exc).__name__}: current log could not be read."
        _write_state(
            conn, source_key=source_key, source_kind="current_log", status="error",
            detail=detail, generation=previous["generation"] if previous else None,
            byte_offset=previous["byte_offset"] if previous else 0,
            records_seen=previous["records_seen"] if previous else 0,
            linked_calls=previous["linked_calls"] if previous else 0,
            unlinked_calls=previous["unlinked_calls"] if previous else 0)
        conn.commit()
        return {"source": source_key, "status": "error", "calls": 0,
                "detail": detail}


def import_local_usage(conn, *, current_root: Path = CURRENT_ROOT,
                       historical_stores: dict[str, Path] | None = None) -> list[dict]:
    """Import supported local usage sources without writing to source files."""
    outcomes = []
    stores = HISTORICAL_STORES if historical_stores is None else historical_stores
    for key, path in stores.items():
        outcomes.append(_import_historical_store(conn, key, path))
    if current_root.is_dir():
        for path in sorted(current_root.glob("*/events.jsonl")):
            outcomes.append(_import_current_log(conn, path))
    else:
        _write_state(
            conn, source_key="copilot-session-root", source_kind="current_log",
            status="unavailable", detail="Current session log folder is not present.",
            generation=None)
        conn.commit()
        outcomes.append({"source": "copilot-session-root", "status": "unavailable",
                         "calls": 0})
    return outcomes
