"""Prompt Spider — local search UI.

Read-only over prompts.db. Binds to 127.0.0.1 only: the corpus contains
customer and MIP-labelled content and must not be reachable off-box.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

import db

app = FastAPI(title="Prompt Spider")
STATIC = Path(__file__).parent / "static"

FTS_SPECIAL = re.compile(r'["*^]')


def fts_query(raw: str) -> str:
    """Make user input safe for FTS5 MATCH, keeping simple prefix search."""
    cleaned = FTS_SPECIAL.sub(" ", raw).strip()
    terms = [t for t in cleaned.split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def csv_list(raw: str | None) -> list[str]:
    """Split a repeatable filter value. Single values still work unchanged."""
    if not raw:
        return []
    return [v.strip() for v in raw.split(",") if v.strip()]


# Grouping is done in SQL, not in the browser, so that it survives pagination.
# Grouping only the rows already loaded would silently regroup every time
# "Load more" ran, and a group header would describe the page rather than the
# result set. Ordering by the group key first keeps each group contiguous
# across pages.
#
#   expr   what identifies the group (also returned to the client)
#   order  how the GROUPS are ordered relative to each other
#   within how rows are ordered inside a group; None = use the chosen sort
GROUPS = {
    "source":  {"expr": "p.source",
                "order": "p.source ASC",
                "within": None},
    "origin":  {"expr": "p.origin",
                "order": "p.origin ASC",
                "within": None},
    "day":     {"expr": "substr(COALESCE(p.ts_source, p.first_seen_at),1,10)",
                "order": "substr(COALESCE(p.ts_source, p.first_seen_at),1,10) DESC",
                "within": None},
    "context": {"expr": "COALESCE(NULLIF(p.app_context,''),'(none)')",
                "order": "COALESCE(NULLIF(p.app_context,''),'(none)') ASC",
                "within": None},
    # Conversations are ordered by their most recent turn, so the list reads
    # newest-thread-first, and turns within a thread stay in the order they
    # were written regardless of the chosen sort — a transcript out of
    # sequence is not a useful thing to look at.
    "session": {"expr": "p.source_session_id",
                "order": ("MAX(COALESCE(p.ts_source, p.first_seen_at))"
                          " OVER (PARTITION BY p.source_session_id) DESC,"
                          " p.source_session_id ASC"),
                "within": "p.turn_index ASC"},
}

SORTS = {
    "recent": "COALESCE(p.ts_source, p.first_seen_at) DESC",
    "oldest": "COALESCE(p.ts_source, p.first_seen_at) ASC",
    "title": "p.title COLLATE NOCASE ASC",
    # No index backs these, but the table is ~26k rows on a local box: the
    # scan costs a few milliseconds and is worth it for "what did I write at
    # length about?", which is otherwise unanswerable.
    "longest": "LENGTH(p.prompt) DESC",
    "shortest": "LENGTH(p.prompt) ASC",
    "cost": ("CASE WHEN (SELECT SUM_NANO_TEXT(u.total_nano_aiu)"
             " FROM prompt_usage_calls u WHERE u.prompt_id=p.id"
             " AND u.total_nano_aiu IS NOT NULL) IS NULL THEN 1 ELSE 0 END,"
             "(SELECT SUM_NANO_TEXT(u.total_nano_aiu) FROM prompt_usage_calls u"
             " WHERE u.prompt_id=p.id AND u.total_nano_aiu IS NOT NULL)"
             " COLLATE DECIMAL_TEXT DESC"),
}


def _nano_expr(prompt_alias="p"):
    return (f"(SELECT SUM_NANO_TEXT(u.total_nano_aiu) FROM prompt_usage_calls u"
            f" WHERE u.prompt_id={prompt_alias}.id AND u.total_nano_aiu IS NOT NULL)")


def build_where(source, origin, context, agent, frm, to, min_credits=None):
    """Filter clauses shared by search, export and facets."""
    where, args = [], []

    sources = csv_list(source)
    if sources:
        where.append(f"p.source IN ({','.join('?' * len(sources))})")
        args += sources

    # "human" is the default view; the raw corpus is 93% agent scaffolding.
    # Reference `origin` directly rather than COALESCE-ing it, so the browse
    # index applies. _migrate guarantees the column is never NULL.
    origins = csv_list(origin)
    if origins and "any" not in origins:
        if "nonhuman" in origins:
            rest = [o for o in origins if o != "nonhuman"]
            clause = "p.origin <> 'human'"
            if rest:
                clause = (f"({clause} OR p.origin IN ({','.join('?' * len(rest))}))")
                args += rest
            where.append(clause)
        else:
            where.append(f"p.origin IN ({','.join('?' * len(origins))})")
            args += origins

    contexts = csv_list(context)
    if contexts:
        where.append(f"p.app_context IN ({','.join('?' * len(contexts))})")
        args += contexts

    if agent == "only":
        where.append("p.agent_id IS NOT NULL")
    elif agent == "none":
        where.append("p.agent_id IS NULL")
    if frm:
        where.append("COALESCE(p.ts_source, p.ts_observed) >= ?"); args.append(frm)
    if to:
        where.append("COALESCE(p.ts_source, p.ts_observed) <= ?"); args.append(to + "T23:59:59")
    if min_credits is not None:
        where.append(f"NANO_AT_LEAST({_nano_expr()}, ?) = 1")
        args.append(min_credits)
    return where, args


def _credits(nano: str | None) -> str | None:
    if nano is None:
        return None
    whole, fractional = divmod(int(nano), 1_000_000_000)
    fraction = f"{fractional:09d}".rstrip("0")
    return f"{whole}.{fraction}" if fraction else str(whole)


def _usd(credits: str | None) -> str | None:
    if credits is None:
        return None
    whole, _, fractional = credits.partition(".")
    digits = whole + fractional
    places = len(fractional) + 2
    digits = digits.rjust(places + 1, "0")
    value = digits[:-places] + "." + digits[-places:]
    return value.rstrip("0").rstrip(".") or "0"


def _source_state_for_call(conn, source_kind: str, source_key: str):
    key = f"copilot-session:{source_key}" if source_kind == "current_log" else source_key
    return conn.execute(
        "SELECT status, last_success_at, source_updated_at FROM cost_import_state"
        " WHERE source_key=?", (key,)).fetchone()


def _is_stale_state(state) -> bool:
        if not state or state["status"] != "ok":
            return True
        modified = state["source_updated_at"]
        try:
            when = (datetime.fromtimestamp(int(modified) / 1_000_000_000, timezone.utc)
                    if modified and str(modified).isdigit()
                    else datetime.fromisoformat(modified.replace("Z", "+00:00")))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds() > 86400
        except (TypeError, ValueError, OSError):
            return True


def _attach_prompt_costs(conn, rows):
    ids = [r["id"] for r in rows]
    if not ids:
        return rows
    marks = ",".join("?" * len(ids))
    totals = {
        r["prompt_id"]: dict(r) for r in conn.execute(
            "SELECT prompt_id, SUM_NANO_TEXT(total_nano_aiu) AS known_nano,"
            " COUNT(*) AS calls,"
            " SUM(CASE WHEN total_nano_aiu IS NULL THEN 1 ELSE 0 END) AS missing_calls,"
            " SUM(CASE WHEN status NOT IN ('recorded','complete') THEN 1 ELSE 0 END)"
            " AS partial_calls, MAX(event_at) AS last_usage_at"
            f" FROM prompt_usage_calls WHERE prompt_id IN ({marks}) GROUP BY prompt_id",
            ids)
    }
    for row in rows:
        item = totals.get(row["id"])
        if not item:
            row.update(local_credits=None, local_usd_equivalent=None, cost_calls=0,
                       cost_basis="locally_reported_usage", cost_status="unknown",
                       last_usage_at=None)
            continue
        states = [ _source_state_for_call(conn, c["source_kind"], c["source_key"])
                   for c in conn.execute(
                       "SELECT DISTINCT source_kind, source_key FROM prompt_usage_calls"
                       " WHERE prompt_id=?", (row["id"],))]
        stale = any(_is_stale_state(s) for s in states)
        if item["known_nano"] is None:
            status = "unknown"
        elif item["missing_calls"] or item["partial_calls"]:
            status = "partial"
        elif stale:
            status = "stale"
        else:
            status = "recorded"
        credits = _credits(item["known_nano"])
        row.update(local_credits=credits,
                   local_usd_equivalent=_usd(credits),
                   cost_calls=item["calls"],
                   cost_basis="locally_reported_usage",
                   cost_status=status,
                   last_usage_at=item["last_usage_at"])
    return rows


def _utc_bound(value: str | None, *, end=False) -> str | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            parsed = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="microseconds")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid UTC date: {value}") from exc


def _cost_filters(frm: str | None, to: str | None, source: str | None,
                  model: str | None):
    clauses, args = [], []
    start, end = _utc_bound(frm), _utc_bound(to)
    if start:
        clauses.append("event_at >= ?"); args.append(start)
    if end:
        clauses.append("event_at < ?"); args.append(end)
    sources = csv_list(source)
    if sources:
        clauses.append(f"prompt_source IN ({','.join('?' * len(sources))})")
        args.extend(sources)
    if model:
        clauses.append("model = ?"); args.append(model)
    return clauses, args


def _query_sources(conn):
    latest = conn.execute(
        "SELECT source_kind, source_key, MAX(event_at) AS newest"
        " FROM prompt_usage_calls GROUP BY source_kind, source_key")
    newest = {(r["source_kind"], r["source_key"]): r["newest"] for r in latest}
    now = datetime.now(timezone.utc)
    grouped = {}
    labels = {"current_log": "Current Copilot session logs",
              "historical_store": "Scout and Copilot CLI history"}
    for row in conn.execute(
            "SELECT source_key, source_kind, status, detail, last_success_at,"
            " source_updated_at FROM cost_import_state"):
        raw_modified = row["source_updated_at"]
        if raw_modified and str(raw_modified).isdigit():
            modified = datetime.fromtimestamp(
                int(raw_modified) / 1_000_000_000, timezone.utc
            ).isoformat(timespec="microseconds")
        else:
            modified = _utc_bound(raw_modified)
        age_stale = False
        if modified:
            age_stale = (now - datetime.fromisoformat(modified)).total_seconds() > 86400
        status = row["status"]
        if status == "ok" and age_stale:
            status = "stale"
        group = grouped.setdefault(row["source_kind"], {
            "source": labels.get(row["source_kind"], "Local usage source"),
            "statuses": [], "last_success": [], "source_updates": [],
            "newest_events": [], "warning_count": 0, "source_count": 0,
            "warnings": [],
        })
        group["source_count"] += 1
        group["statuses"].append(status)
        if status != "ok":
            group["warning_count"] += 1
            if row["detail"]:
                group["warnings"].append(row["detail"])
        if row["last_success_at"]:
            group["last_success"].append(row["last_success_at"])
        if modified:
            group["source_updates"].append(modified)
        event_at = newest.get((row["source_kind"], row["source_key"]))
        if event_at:
            group["newest_events"].append(event_at)
    out = []
    for group in grouped.values():
        states = group.pop("statuses")
        status = ("replaced" if "replaced" in states else
                  "error" if "error" in states else
                  "unavailable" if all(s == "unavailable" for s in states) else
                  "partial" if any(s in ("partial", "unavailable") for s in states) else
                  "stale" if "stale" in states else "ok")
        out.append({
            "source": group["source"], "status": status,
            "source_count": group["source_count"],
            "warning_count": group["warning_count"],
            "warnings": sorted(set(group["warnings"]))[:5],
            "last_success_at": max(group["last_success"], default=None),
            "source_updated_at": max(group["source_updates"], default=None),
            "newest_event_at": max(group["newest_events"], default=None),
        })
    return out


def search(q: str, source: str | None, context: str | None, agent: str | None,
           frm: str | None, to: str | None, sort: str, limit: int, offset: int,
           origin: str | None = None, group: str | None = None,
           min_credits: str | None = None):
    conn = db.connect()
    where, args = build_where(source, origin, context, agent, frm, to, min_credits)

    if group == "session":
        rows, total, next_offset, unit = _search_by_session(
            conn, q, where, args, limit, offset)
        return _attach_prompt_costs(conn, rows), total, next_offset, unit

    order = SORTS.get(sort, SORTS["recent"])
    grp = GROUPS.get(group or "")
    group_expr = grp["expr"] if grp else "NULL"
    if grp:
        order = f"{grp['order']}, {grp['within'] or order}"

    if q.strip():
        match = fts_query(q)
        if sort == "relevance" and not grp:
            order = "bm25(prompts_fts) ASC"
        elif sort == "relevance":
            order = f"{grp['order']}, {grp['within'] or 'bm25(prompts_fts) ASC'}"
        sql = f"""
            SELECT p.*, snippet(prompts_fts, 0, '<mark>', '</mark>', '…', 24) AS snip,
                   {group_expr} AS grp
            FROM prompts_fts
            JOIN prompts p ON p.rowid = prompts_fts.rowid
            WHERE prompts_fts MATCH ?
            {'AND ' + ' AND '.join(where) if where else ''}
            ORDER BY {order} LIMIT ? OFFSET ?"""
        params = [match, *args, limit, offset]
        count_sql = f"""SELECT COUNT(*) FROM prompts_fts
            JOIN prompts p ON p.rowid = prompts_fts.rowid
            WHERE prompts_fts MATCH ?
            {'AND ' + ' AND '.join(where) if where else ''}"""
        count_params = [match, *args]
    else:
        if sort == "relevance" and not grp:
            order = SORTS["recent"]
        sql = f"""SELECT p.*, NULL AS snip, {group_expr} AS grp FROM prompts p
            {'WHERE ' + ' AND '.join(where) if where else ''}
            ORDER BY {order} LIMIT ? OFFSET ?"""
        params = [*args, limit, offset]
        count_sql = f"SELECT COUNT(*) FROM prompts p {'WHERE ' + ' AND '.join(where) if where else ''}"
        count_params = args

    rows = _attach_prompt_costs(conn, [dict(r) for r in conn.execute(sql, params)])
    total = conn.execute(count_sql, count_params).fetchone()[0]
    return rows, total, offset + len(rows), "prompts"


# Sessions are paginated as WHOLE CONVERSATIONS, not as rows.
#
# The obvious implementation — ORDER BY MAX(ts) OVER (PARTITION BY session) —
# is correct but forces SQLite to materialise and sort every matching row
# before it can return the first fifty: measured at 8.6s on a 32k-row corpus,
# and far worse while a harvest holds the write lock.
#
# Picking the page of conversations first turns that into a grouped aggregate
# (~40ms) plus an indexed lookup of a handful of session ids. It also matches
# what the view is for: "show me twenty more conversations" is the useful unit
# here, and it guarantees a conversation is never split across a page boundary.
SESSION_PAGE = 20


def _search_by_session(conn, q, where, args, limit, offset):
    ts = "COALESCE(p.ts_source, p.first_seen_at)"
    n_sessions = max(1, min(SESSION_PAGE, limit))
    clause = " AND ".join(where)

    if q.strip():
        base = ("FROM prompts_fts JOIN prompts p ON p.rowid = prompts_fts.rowid"
                " WHERE prompts_fts MATCH ?" + (f" AND {clause}" if where else ""))
        head = [fts_query(q), *args]
    else:
        base = "FROM prompts p" + (f" WHERE {clause}" if where else "")
        head = list(args)

    page = conn.execute(
        f"SELECT p.source_session_id, MAX({ts}) m {base}"
        " GROUP BY 1 ORDER BY m DESC, p.source_session_id ASC LIMIT ? OFFSET ?",
        [*head, n_sessions, offset]).fetchall()
    if not page:
        total = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 {base} GROUP BY p.source_session_id)",
            head).fetchone()[0]
        return [], total, offset, "conversations"

    ids = [r[0] for r in page]
    rank = {sid: i for i, sid in enumerate(ids)}
    marks = ",".join("?" * len(ids))
    if q.strip():
        sql = (f"SELECT p.*, snippet(prompts_fts, 0, '<mark>', '</mark>', '…', 24) AS snip,"
               f" p.source_session_id AS grp {base} AND p.source_session_id IN ({marks})")
    else:
        sql = (f"SELECT p.*, NULL AS snip, p.source_session_id AS grp {base}"
               + (" AND" if where else " WHERE") + f" p.source_session_id IN ({marks})")
    rows = [dict(r) for r in conn.execute(sql, [*head, *ids])]
    # Order in Python: the page order is already decided, and re-deriving it in
    # SQL would mean re-joining the aggregate for no gain.
    rows.sort(key=lambda r: (rank[r["source_session_id"]], r["turn_index"] or 0))

    total = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 {base} GROUP BY p.source_session_id)",
        head).fetchone()[0]
    return rows, total, offset + len(ids), "conversations"


@app.get("/api/health")
def health():
    conn = db.connect()
    return {"ok": True, "prompts": conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]}


@app.get("/api/search")
def api_search(q: str = "", source: str | None = None, context: str | None = None,
               agent: str | None = None, frm: str | None = None, to: str | None = None,
               sort: str = "relevance", limit: int = Query(50, le=500), offset: int = 0,
               origin: str | None = "human", group: str | None = None,
               min_credits: str | None = None):
    if min_credits is not None:
        try:
            value = Decimal(min_credits)
            if not value.is_finite() or value < 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            raise HTTPException(status_code=400, detail="min_credits must be a non-negative decimal.")
    rows, total, next_offset, unit = search(
        q, source, context, agent, frm, to, sort, limit, offset, origin, group,
        min_credits)
    # next_offset is returned rather than inferred by the client: grouping by
    # conversation pages whole conversations, so the number of rows on a page
    # says nothing about how far to advance.
    return {"total": total, "count": len(rows), "results": rows,
            "next_offset": next_offset, "unit": unit}


@app.get("/api/facets")
def api_facets(q: str = "", source: str | None = None, context: str | None = None,
               agent: str | None = None, frm: str | None = None, to: str | None = None,
               origin: str | None = "human", group: str | None = None):
    """Counts per group for the CURRENT filter.

    The group headers in the UI need a true total, not the number of rows that
    happen to be loaded. Without this a header would read "12" on the first
    page and "31" after Load more, which is worse than showing nothing.

    Source and origin counts ignore their own filter, so the filter chips can
    show what selecting them would yield rather than only what is already
    selected.
    """
    conn = db.connect()

    def counts(group_expr, ignore_source=False, ignore_origin=False, limit=None):
        where, args = build_where(
            None if ignore_source else source,
            None if ignore_origin else origin,
            context, agent, frm, to)
        tail = " GROUP BY 1 ORDER BY 2 DESC" + (f" LIMIT {int(limit)}" if limit else "")
        if q.strip():
            sql = (f"SELECT {group_expr} k, COUNT(*) n FROM prompts_fts"
                   " JOIN prompts p ON p.rowid = prompts_fts.rowid"
                   " WHERE prompts_fts MATCH ?"
                   + (" AND " + " AND ".join(where) if where else "") + tail)
            params = [fts_query(q), *args]
        else:
            sql = (f"SELECT {group_expr} k, COUNT(*) n FROM prompts p"
                   + (" WHERE " + " AND ".join(where) if where else "") + tail)
            params = args
        return {str(r[0]): r[1] for r in conn.execute(sql, params)}

    out = {
        "by_source": counts("p.source", ignore_source=True),
        "by_origin": counts("COALESCE(p.origin,'human')", ignore_origin=True),
    }
    grp = GROUPS.get(group or "")
    # Grouping by conversation produces one key per thread — thousands of them,
    # nearly all counting 1-3 turns. Building and shipping that dict cost more
    # than the search it decorates, so it is skipped: the header already names
    # the conversation, and a per-thread total adds nothing. Other groupings are
    # capped for the same reason, since only the visible headers are ever read.
    if grp and group != "session":
        out["by_group"] = counts(grp["expr"], limit=200)
    return out


@app.get("/api/stats")
def api_stats():
    conn = db.connect()
    by_source = {r[0]: r[1] for r in conn.execute(
        "SELECT source, COUNT(*) FROM prompts GROUP BY 1")}
    by_origin = {r[0]: r[1] for r in conn.execute(
        "SELECT COALESCE(origin,'human'), COUNT(*) FROM prompts GROUP BY 1")}
    discovered, pending = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN last_harvested_at IS NULL THEN 1 ELSE 0 END), 0)"
        " FROM harvest_state").fetchone()
    runs = [dict(r) for r in conn.execute(
        "SELECT run_id, started_at, ended_at, status, mode, enumerated, visited,"
        " items_new, empty_ratio, error FROM harvest_runs"
        " ORDER BY started_at DESC LIMIT 12")]
    prompts = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    sessions = conn.execute("SELECT COUNT(DISTINCT source_session_id) FROM prompts").fetchone()[0]
    agents = conn.execute("SELECT COUNT(*) FROM prompts WHERE agent_id IS NOT NULL").fetchone()[0]
    human = conn.execute("SELECT COUNT(*) FROM prompts WHERE COALESCE(origin,'human')='human'").fetchone()[0]
    return {"prompts": prompts, "sessions": sessions, "agent_prompts": agents,
            "human": human, "by_source": by_source, "by_origin": by_origin,
            "state": {"discovered": discovered, "pending": pending},
            "runs": runs}


@app.get("/api/costs/summary")
def api_cost_summary(frm: str | None = Query(None, alias="from"),
                     to: str | None = None, source: str | None = None,
                     model: str | None = None):
    """Local usage totals and coverage; these values are not an invoice."""
    conn = db.connect()
    where, args = _cost_filters(frm, to, source, model)
    clause = " WHERE " + " AND ".join(where) if where else ""
    row = conn.execute(
        "SELECT COUNT(*) AS calls,"
        " SUM_NANO_TEXT(total_nano_aiu) AS known_nano,"
        " SUM(CASE WHEN total_nano_aiu IS NULL THEN 1 ELSE 0 END) AS missing_calls,"
        " SUM(CASE WHEN prompt_id IS NOT NULL AND link_status='linked' THEN 1 ELSE 0 END)"
        " AS linked_calls,"
        " SUM(CASE WHEN prompt_id IS NULL OR link_status<>'linked' THEN 1 ELSE 0 END)"
        " AS unlinked_calls,"
        " SUM(CASE WHEN prompt_id IS NOT NULL AND link_status='linked'"
        "           AND total_nano_aiu IS NOT NULL THEN 1 ELSE 0 END) AS linked_known_calls,"
        " SUM(CASE WHEN status NOT IN ('recorded','complete') THEN 1 ELSE 0 END)"
        " AS partial_calls FROM prompt_usage_calls" + clause, args).fetchone()
    linked = conn.execute(
        "SELECT SUM_NANO_TEXT(total_nano_aiu) FROM prompt_usage_calls"
        + (clause + " AND " if clause else " WHERE ")
        + "prompt_id IS NOT NULL AND link_status='linked'",
        args).fetchone()[0]
    unlinked = conn.execute(
        "SELECT SUM_NANO_TEXT(total_nano_aiu) FROM prompt_usage_calls"
        + (clause + " AND " if clause else " WHERE ")
        + "(prompt_id IS NULL OR link_status<>'linked')", args).fetchone()[0]

    def breakdown(expression):
        return [dict(r) for r in conn.execute(
            "SELECT " + expression + " AS name, COUNT(*) AS calls,"
            " SUM(CASE WHEN total_nano_aiu IS NULL THEN 1 ELSE 0 END) AS missing_calls,"
            " SUM_NANO_TEXT(total_nano_aiu) AS nano_credits"
            " FROM prompt_usage_calls" + clause
            + " GROUP BY 1 ORDER BY name LIMIT 500", args)]

    day = breakdown("COALESCE(substr(event_at,1,10),'Unknown date')")
    by_model = breakdown("COALESCE(model,'Unknown model')")
    by_source = breakdown("COALESCE(prompt_source,'Unknown source')")
    supported = ["copilot-app", "scout", "copilot-cli"]
    coverage_where = [f"source IN ({','.join('?' * len(supported))})"]
    coverage_args: list = list(supported)
    start, end = _utc_bound(frm), _utc_bound(to)
    if start:
        coverage_where.append("COALESCE(ts_source,ts_observed) >= ?")
        coverage_args.append(start)
    if end:
        coverage_where.append("COALESCE(ts_source,ts_observed) < ?")
        coverage_args.append(end)
    selected_sources = csv_list(source)
    if selected_sources:
        coverage_where.append(f"source IN ({','.join('?' * len(selected_sources))})")
        coverage_args.extend(selected_sources)
    denominator = conn.execute(
        "SELECT COUNT(*) FROM prompts WHERE " + " AND ".join(coverage_where),
        coverage_args).fetchone()[0]
    linked_prompts = conn.execute(
        "SELECT COUNT(DISTINCT prompt_id) FROM prompt_usage_calls"
        + (clause + " AND " if clause else " WHERE ")
        + "prompt_id IS NOT NULL AND link_status='linked'", args).fetchone()[0]
    known_prompts = conn.execute(
        "SELECT COUNT(DISTINCT prompt_id) FROM prompt_usage_calls"
        + (clause + " AND " if clause else " WHERE ")
        + "prompt_id IS NOT NULL AND link_status='linked' AND total_nano_aiu IS NOT NULL",
        args).fetchone()[0]
    pct = (format(Decimal(known_prompts) * 100 / Decimal(denominator), ".1f")
           if denominator else None)
    all_nano = row["known_nano"]
    statuses = _query_sources(conn)
    return {
        "basis": "locally_reported_usage",
        "currency": "USD",
        "credits_to_usd": "0.01",
        "label": "Locally recorded; not an invoice amount",
        "period": {"from_inclusive_utc": start, "to_exclusive_utc": end},
        "local_credits": _credits(all_nano),
        "local_usd_equivalent": _usd(_credits(all_nano)),
        "nano_credits": all_nano,
        "linked_nano_credits": linked,
        "unlinked_nano_credits": unlinked,
        "linked_credits": _credits(linked),
        "unlinked_credits": _credits(unlinked),
        "calls": row["calls"] or 0,
        "linked_calls": row["linked_calls"] or 0,
        "unlinked_calls": row["unlinked_calls"] or 0,
        "missing_amount_calls": row["missing_calls"] or 0,
        "partial_calls": row["partial_calls"] or 0,
        "coverage": {"supported_prompt_sources": supported,
                     "denominator": denominator, "linked_prompts": linked_prompts,
                     "prompts_with_known_cost": known_prompts,
                     "known_cost_percent": pct},
        "by_day": day,
        "by_model": by_model,
        "by_source": by_source,
        "source_freshness": statuses,
        "unsupported_sources": ["M365 Copilot", "Cowork", "VS Code",
                                "GitHub Copilot web and desktop"],
    }


@app.get("/api/costs/top")
def api_cost_top(limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0),
                 frm: str | None = Query(None, alias="from"),
                 to: str | None = None, source: str | None = None,
                 model: str | None = None, q: str = "",
                 sort: str = "credits", direction: str = "desc"):
    conn = db.connect()
    where, args = _cost_filters(frm, to, source, model)
    if where:
        where = [f"u.{clause}" for clause in where]
    where.append("u.prompt_id=p.id AND u.link_status='linked'")
    query = q.strip().casefold()
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(lower(COALESCE(p.title,'')) LIKE ? ESCAPE '\\' OR "
                     "lower(p.prompt) LIKE ? ESCAPE '\\' OR "
                     "lower(p.source) LIKE ? ESCAPE '\\')")
        args.extend([pattern] * 3)
    if sort not in {"credits", "calls", "title", "date", "source"}:
        raise HTTPException(status_code=400, detail="Unsupported prompt cost sort.")
    if direction not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="Direction must be asc or desc.")
    sort_column = {
        "credits": "known_nano COLLATE DECIMAL_TEXT",
        "calls": "cost_calls",
        "title": "title COLLATE NOCASE",
        "date": "prompt_at",
        "source": "source COLLATE NOCASE",
    }[sort]
    clause = " WHERE " + " AND ".join(where)
    grouped = (
        "SELECT p.id, p.source, p.title, COALESCE(p.ts_source,p.first_seen_at) AS prompt_at,"
        " SUM_NANO_TEXT(u.total_nano_aiu) AS known_nano, COUNT(*) AS cost_calls,"
        " SUM(CASE WHEN u.total_nano_aiu IS NULL THEN 1 ELSE 0 END) AS missing_calls,"
        " SUM(CASE WHEN u.call_kind='helper' THEN 1 ELSE 0 END) AS helper_calls,"
        " SUM(CASE WHEN u.status NOT IN ('recorded','complete') THEN 1 ELSE 0 END)"
        " AS partial_calls FROM prompts p JOIN prompt_usage_calls u"
        + clause + " GROUP BY p.id"
        " HAVING SUM_NANO_TEXT(u.total_nano_aiu) IS NOT NULL")
    rows = [dict(r) for r in conn.execute(
        "SELECT ranked.*, COUNT(*) OVER() AS total_count FROM ("
        + grouped + ") AS ranked"
        f" ORDER BY {sort_column} {direction.upper()}, title COLLATE NOCASE ASC, id ASC"
        " LIMIT ? OFFSET ?", [*args, limit, offset])]
    if rows:
        total = rows[0].pop("total_count")
        for row in rows[1:]:
            row.pop("total_count")
    elif offset:
        # A caller can request a page beyond the current end; return the real
        # total so clients can recover instead of treating it as an empty set.
        total = conn.execute(
            "SELECT COUNT(*) FROM (" + grouped + ")", args).fetchone()[0]
    else:
        total = 0
    for row in rows:
        credits = _credits(row["known_nano"])
        freshness = conn.execute(
            "SELECT DISTINCT source_kind, source_key FROM prompt_usage_calls"
            " WHERE prompt_id=?", (row["id"],)).fetchall()
        stale = any(_is_stale_state(_source_state_for_call(
            conn, source["source_kind"], source["source_key"])) for source in freshness)
        row.update(local_credits=credits, local_usd_equivalent=_usd(credits),
                   cost_basis="locally_reported_usage",
                   cost_status=("partial" if row["missing_calls"] or row["partial_calls"]
                                else "stale" if stale else "recorded"))
    return {"basis": "locally_reported_usage",
            "label": "Locally recorded; not an invoice amount",
            "results": rows, "limit": limit, "offset": offset,
            "total": total, "count": len(rows),
            "next_offset": offset + len(rows)}


@app.get("/api/costs/breakdown")
def api_cost_breakdown(group: str, q: str = "", sort: str = "name",
                       direction: str = "asc",
                       limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0),
                       frm: str | None = Query(None, alias="from"),
                       to: str | None = None, source: str | None = None,
                       model: str | None = None):
    """Filter and sort one complete cost breakdown before returning its page."""
    conn = db.connect()
    expressions = {
        "day": "COALESCE(substr(event_at,1,10),'Unknown date')",
        "model": "COALESCE(model,'Unknown model')",
        "source": "COALESCE(prompt_source,'Unknown source')",
    }
    if group not in expressions:
        raise HTTPException(status_code=400, detail="Unsupported cost breakdown.")
    if sort not in {"name", "credits", "calls"}:
        raise HTTPException(status_code=400, detail="Unsupported breakdown sort.")
    if direction not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="Direction must be asc or desc.")
    expression = expressions[group]
    where, args = _cost_filters(frm, to, source, model)
    query = q.strip().casefold()
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(f"lower({expression}) LIKE ? ESCAPE '\\'")
        args.append(pattern)
    clause = " WHERE " + " AND ".join(where) if where else ""
    grouped = (
        "SELECT " + expression + " AS name, COUNT(*) AS calls,"
        " SUM(CASE WHEN total_nano_aiu IS NULL THEN 1 ELSE 0 END) AS missing_calls,"
        " SUM_NANO_TEXT(total_nano_aiu) AS nano_credits"
        " FROM prompt_usage_calls" + clause + " GROUP BY 1")
    order_column = {
        "name": "name COLLATE NOCASE",
        "credits": "nano_credits COLLATE DECIMAL_TEXT",
        "calls": "calls",
    }[sort]
    # Unknown amounts stay last in either direction.
    order = (f"CASE WHEN nano_credits IS NULL THEN 1 ELSE 0 END ASC, "
             f"{order_column} {direction.upper()}, name COLLATE NOCASE ASC")
    total = conn.execute(
        "SELECT COUNT(*) FROM (" + grouped + ")", args).fetchone()[0]
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM (" + grouped + f") ORDER BY {order} LIMIT ? OFFSET ?",
        [*args, limit, offset])]
    return {"group": group, "results": rows, "total": total,
            "limit": limit, "offset": offset, "count": len(rows),
            "next_offset": offset + len(rows)}


@app.get("/api/costs/prompt/{prompt_id}")
def api_cost_prompt(prompt_id: str, limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0)):
    conn = db.connect()
    prompt = conn.execute(
        "SELECT id, source, title, COALESCE(ts_source,first_seen_at) AS prompt_at,"
        " origin FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt was not found.")
    total = conn.execute(
        "SELECT COUNT(*) FROM prompt_usage_calls WHERE prompt_id=?", (prompt_id,)
    ).fetchone()[0]
    calls = [dict(r) for r in conn.execute(
        "SELECT source_kind, prompt_source, link_status, link_reason, call_kind, model,"
        " initiator, total_nano_aiu, credits, usd_equivalent, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
        " token_details_json, event_at, status"
        " FROM prompt_usage_calls WHERE prompt_id=?"
        " ORDER BY event_at, source_kind LIMIT ? OFFSET ?",
        (prompt_id, limit, offset))]
    for call in calls:
        try:
            call["recorded_rates"] = json.loads(call.pop("token_details_json") or "null")
        except json.JSONDecodeError:
            call["recorded_rates"] = None
            call["rates_status"] = "unreadable"
    totals = conn.execute(
        "SELECT SUM_NANO_TEXT(total_nano_aiu) AS known_nano,"
        " SUM(CASE WHEN total_nano_aiu IS NULL THEN 1 ELSE 0 END) AS missing,"
        " SUM(CASE WHEN call_kind='helper' THEN 1 ELSE 0 END) AS helpers,"
        " SUM(CASE WHEN call_kind='direct' THEN 1 ELSE 0 END) AS direct,"
        " SUM(CASE WHEN status NOT IN ('recorded','complete') THEN 1 ELSE 0 END) AS partial"
        " FROM prompt_usage_calls WHERE prompt_id=?", (prompt_id,)).fetchone()
    credits = _credits(totals["known_nano"])
    freshness = [dict(r) for r in conn.execute(
        "SELECT DISTINCT source_kind, source_key FROM prompt_usage_calls WHERE prompt_id=?",
        (prompt_id,))]
    fresh = []
    for row in freshness:
        state = _source_state_for_call(conn, row["source_kind"], row["source_key"])
        fresh.append({"source": ("Current Copilot session logs"
                                 if row["source_kind"] == "current_log"
                                 else "Scout and Copilot CLI history"),
                      "status": ("stale" if _is_stale_state(state) else state["status"]),
                      "last_success_at": state["last_success_at"] if state else None})
    return {
        "prompt": dict(prompt), "basis": "locally_reported_usage",
        "label": "Locally recorded; not an invoice amount",
        "currency": "USD", "credits_to_usd": "0.01",
        "local_credits": credits, "local_usd_equivalent": _usd(credits),
        "cost_status": ("unknown" if credits is None else
                        "partial" if totals["missing"] or totals["partial"] else "recorded"),
        "coverage": {"calls": total, "direct_calls": totals["direct"] or 0,
                     "helper_calls": totals["helpers"] or 0,
                     "missing_amount_calls": totals["missing"] or 0},
        "source_freshness": fresh,
        "calls": calls, "count": len(calls), "total_calls": total,
        "next_offset": offset + len(calls),
    }


@app.get("/api/costs/export.csv")
def api_cost_export(frm: str | None = Query(None, alias="from"),
                    to: str | None = None, source: str | None = None,
                    model: str | None = None,
                    limit: int = Query(50000, ge=1, le=50000)):
    conn = db.connect()
    where, args = _cost_filters(frm, to, source, model)
    clause = " WHERE " + " AND ".join(where) if where else ""
    cols = ["event_at", "prompt_source", "model", "initiator", "call_kind",
            "link_status", "link_reason", "total_nano_aiu", "credits",
            "usd_equivalent", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "reasoning_tokens", "status"]
    rows = conn.execute(
        "SELECT " + ", ".join(cols) + " FROM prompt_usage_calls"
        + clause + " ORDER BY event_at LIMIT ?", [*args, limit])
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for col in cols:
            value = row[col]
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[col] = value
        writer.writerow(safe)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage-only.csv"'})


@app.get("/api/export")
def api_export(fmt: str = "json", q: str = "", source: str | None = None,
               context: str | None = None, agent: str | None = None,
               frm: str | None = None, to: str | None = None,
               sort: str = "relevance", limit: int = Query(5000, le=50000),
               origin: str | None = "human", group: str | None = None,
               min_credits: str | None = None):
    rows, _, _, _ = search(q, source, context, agent, frm, to, sort, limit, 0,
                           origin, group, min_credits)
    cols = ["ts_source", "first_seen_at", "source", "origin", "app_context",
            "agent_id", "title", "prompt", "response_excerpt", "deep_link", "provenance",
            "local_credits", "local_usd_equivalent", "cost_calls", "cost_basis",
            "cost_status", "last_usage_at"]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in cols})
        return StreamingResponse(io.BytesIO(buf.getvalue().encode("utf-8-sig")),
                                 media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=prompts.csv"})
    if fmt == "md":
        out = ["# Prompt Spider export", ""]
        for r in rows:
            out.append(f"## {r.get('title') or '(untitled)'}")
            out.append(f"*{r['source']} · {r['first_seen_at']}* — [open]({r.get('deep_link') or ''})")
            if r.get("local_credits") is not None:
                out.append(
                    f"Locally recorded usage: {r['local_credits']} credits / "
                    f"${r['local_usd_equivalent']} · {r['cost_status']} · "
                    "not an invoice amount")
            else:
                out.append("Local usage: no cost data")
            out.append("")
            out.append("> " + (r["prompt"] or "").replace("\n", "\n> "))
            out.append("")
        return PlainTextResponse("\n".join(out), media_type="text/markdown")
    return StreamingResponse(
        io.BytesIO(json.dumps([{c: r.get(c) for c in cols} for r in rows],
                              indent=2, ensure_ascii=False).encode()),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=prompts.json"})


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8420)),
                log_level="warning")
