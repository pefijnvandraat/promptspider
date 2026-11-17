"""Prompt Spider — harvest pipeline.

Three modes, separated because enumeration and extraction have wildly different
cost profiles. A full enumeration of the chat sidebar took 7.5 hours and found
6,362 conversations without reaching the end; extraction is ~8s per item. Doing
both in one run means enumeration eats the whole budget and nothing is stored.

  discover     Walk the sidebar and persist every conversation/task to
               harvest_state. Checkpoints every few rounds, so an interrupted
               run keeps its progress. Run this rarely (hours).

  extract      Read pending items from harvest_state and pull their prompts.
               No enumeration at all. Fully resumable, budget-bounded. This is
               what does the actual backfill work, night after night.

  incremental  Shallow enumerate (top of the recency-ordered list only) plus
               extract whatever is new. Cheap, and the normal nightly mode once
               the backfill has caught up.

Guardrails exist because the dominant failure mode for a DOM harvester is a UI
change that quietly writes empty rows, or a page that fails to hydrate and looks
like "nothing to do". Every check below turns silence into a loud failure.
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from urllib.parse import unquote

import db
from browser import (LockHeld, NavNotReady, SignInRequired, browser_page,
                     run_lock)
from collectors import (BASE, CONV_RE, TASK_RE, Item, enumerate_chat,
                        enumerate_cowork, extract_chat, extract_cowork,
                        open_cowork)
from local_sources import COLLECTORS, classify, strip_injected
from cost_usage import import_local_usage
from github_source import (clean_github_title, enumerate_github,
                           extract_github)

EMPTY_RATIO_ABORT = 0.35      # >35% of visited pages yielding nothing = broken
KNOWN_STREAK_STOP = 4         # consecutive all-known rounds before early exit
INCREMENTAL_ROUNDS = 20       # ~350 most recent items; new work is always on top


class GuardrailTripped(RuntimeError):
    pass


# --------------------------------------------------------------------------- state

def href_to_item(source: str, href: str, title: str) -> Item | None:
    if source == "m365-copilot":
        m = CONV_RE.search(href)
        if not m:
            return None
        return Item(source=source, session_id=m.group("guid"), title=title or "",
                    agent_id=m.group("agent"), deep_link=f"{BASE}{href}",
                    app_context="chat")
    m = TASK_RE.search(href)
    if not m:
        return None
    tid = unquote(m.group(1))
    return Item(source=source, session_id=tid, title=title or "",
                deep_link=f"{BASE}/cowork#/task/{tid}", app_context="task")


def record_discovered(conn, source: str, pairs: list[tuple[str, str]],
                      rank_offset: int = 0) -> int:
    """Persist newly-seen items as pending work. Never overwrites harvest data.

    `rank_offset` is the number of items already enumerated in this walk, so a
    conversation's stored recency_rank is its true position in the sidebar. It
    used to be hardcoded to 0 here and rebuilt as 0 again in pending_from_db,
    which left 1,398 conversations sharing 8 distinct rank values — a populated
    column that looked meaningful and was not.
    """
    now = db.now_iso()
    n = 0
    for offset, (href, title) in enumerate(pairs):
        item = href_to_item(source, href, title)
        if not item:
            continue
        conn.execute(
            "INSERT INTO harvest_state (source, source_session_id, title, agent_id,"
            " recency_rank, content_hash, discovered_at, last_harvested_at)"
            " VALUES (?,?,?,?,?,NULL,?,NULL)"
            " ON CONFLICT(source, source_session_id) DO UPDATE SET"
            " title=COALESCE(NULLIF(excluded.title,''), harvest_state.title),"
            " recency_rank=excluded.recency_rank",
            (item.source, item.session_id, item.title, item.agent_id,
             rank_offset + offset, now))
        n += 1
    conn.commit()
    return n


def _to_item(row) -> Item:
    src, sid = row["source"], row["source_session_id"]
    agent = row["agent_id"]
    rank = row["recency_rank"] or 0
    title = row["title"] or ""
    if src == "m365-copilot":
        path = (f"/chat/agent/{agent}/conversation/{sid}" if agent
                else f"/chat/conversation/{sid}")
        return Item(src, sid, title, agent, rank, f"{BASE}{path}", "chat")
    return Item(src, sid, title, None, rank, f"{BASE}/cowork#/task/{sid}", "task")


def pending_from_db(conn, source: str | None, limit: int) -> list[Item]:
    """Items discovered but never successfully harvested."""
    sql = ("SELECT source, source_session_id, title, agent_id, recency_rank"
           " FROM harvest_state WHERE last_harvested_at IS NULL")
    args: list = []
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY discovered_at LIMIT ?"
    args.append(limit)
    return [_to_item(r) for r in conn.execute(sql, args)]


def harvested_from_db(conn, source: str | None, limit: int,
                      only_incomplete: bool = True) -> list[Item]:
    """Items already harvested, queued for re-extraction.

    This is the backfill population: conversations stored before the hydration
    payload was used, whose rows therefore have no ts_source, no response and a
    scraped title. Re-extraction enriches them in place — `prompts.id` is
    content-addressed, so the same rows are updated rather than duplicated.

    `only_incomplete` keeps the queue to conversations that would actually gain
    something, so repeated runs converge instead of re-walking the whole corpus.
    """
    sql = ("SELECT hs.source, hs.source_session_id, hs.title, hs.agent_id,"
           "       hs.recency_rank"
           "  FROM harvest_state hs"
           " WHERE hs.last_harvested_at IS NOT NULL")
    args: list = []
    if source:
        sql += " AND hs.source=?"
        args.append(source)
    if only_incomplete:
        sql += (" AND EXISTS (SELECT 1 FROM prompts p"
                "             WHERE p.source = hs.source"
                "               AND p.source_session_id = hs.source_session_id"
                "               AND (p.ts_source IS NULL"
                "                    OR COALESCE(p.response_excerpt,'') = ''))")
    sql += " ORDER BY hs.discovered_at DESC LIMIT ?"
    args.append(limit)
    return [_to_item(r) for r in conn.execute(sql, args)]


def known_hrefs(conn, source: str) -> set[str]:
    """Href fragments already harvested, for incremental early-exit."""
    rows = conn.execute(
        "SELECT source_session_id, agent_id FROM harvest_state"
        " WHERE source=? AND last_harvested_at IS NOT NULL", (source,))
    out = set()
    for sid, agent in rows:
        if source == "m365-copilot":
            out.add(f"/chat/agent/{agent}/conversation/{sid}" if agent
                    else f"/chat/conversation/{sid}")
        else:
            out.add(f"/cowork#/task/{sid}")
    return out


# --------------------------------------------------------------------------- write

def _same_prompt(a: str, b: str) -> bool:
    """Is this the same turn, allowing for extractor differences?

    The payload carries raw markdown; the DOM carried rendered text. Comparing
    bytes would call almost every re-harvested turn a conflict, so we compare a
    whitespace-collapsed, case-folded prefix.
    """
    return " ".join((a or "").split()).lower()[:120] == \
           " ".join((b or "").split()).lower()[:120]


def store(conn, item: Item, blocks: list[dict], provenance: str,
          overwrite_conflicts: bool = False) -> tuple[int, int, int]:
    """Upsert prompts for one item. Returns (new_rows, enriched_rows, conflicts).

    Re-harvesting is enrichment, never replacement. `id` is content-addressed on
    (source|session|turn_index), so a second pass over the same conversation
    lands on the same rows — which is what makes backfilling timestamps and
    responses onto the existing corpus safe.

    Three rules, and the third is the one that makes a backfill safe to run
    without gating it on a dry-run:

      * first_seen_at is never touched. It records when WE first saw the row,
        which stays true regardless of what we learn later.
      * New values only win when they are non-empty, so a failed or degraded
        extraction can never blank out data we already hold.
      * A row is only enriched when the prompt at that turn_index still MATCHES.
        The DOM extractor occasionally stored an attachment JSON blob or a
        stray prefix as the prompt, and it missed turns in the middle of some
        conversations — so an index does not always mean the same turn in both.
        Where they disagree we skip the row rather than write the wrong
        timestamp onto the wrong prompt. Those are reported as conflicts.

    `overwrite_conflicts=True` opts out of that last rule and lets the payload
    replace the stored prompt. Every conflict inspected so far was the DOM
    extractor being wrong — an attachment blob captured as a prompt, a prompt
    truncated to a single character, rendered text where the source had
    markdown — so overwriting is usually the correct call. It stays opt-in
    because it is the only path here that destroys previously harvested text.
    (Distinct from `repair()` below, which re-applies stripping rules to text
    we already hold without going back to the network.)
    """
    now = db.now_iso()
    joined = "\n".join(b.get("prompt", "") for b in blocks)
    chash = db.content_hash(joined)
    new_rows = 0
    enriched = 0
    conflicts = 0

    for idx, blk in enumerate(blocks):
        prompt = (blk.get("prompt") or "").strip()
        if not prompt:
            continue
        pid = db.prompt_id(item.source, item.session_id, idx)
        ts_source = blk.get("ts_source")
        response = (blk.get("response") or "")[:2000]
        title = blk.get("title") or item.title

        existing = conn.execute(
            "SELECT prompt FROM prompts WHERE id=?", (pid,)).fetchone()
        if existing is not None:
            if not _same_prompt(existing["prompt"], prompt):
                if not overwrite_conflicts:
                    conflicts += 1
                    conn.execute("UPDATE prompts SET last_seen_at=? WHERE id=?", (now, pid))
                    continue
                conn.execute("UPDATE prompts SET prompt=? WHERE id=?", (prompt, pid))
            cur = conn.execute(
                "UPDATE prompts SET last_seen_at=?,"
                " ts_source        = COALESCE(?, ts_source),"
                " response_excerpt = COALESCE(NULLIF(?,''), response_excerpt),"
                " title            = COALESCE(NULLIF(?,''), title),"
                " message_id       = COALESCE(?, message_id),"
                " author           = COALESCE(?, author)"
                " WHERE id=?",
                (now, ts_source, response, title,
                 blk.get("message_id"), blk.get("author"), pid))
            if cur.rowcount:
                enriched += 1
            continue

        conn.execute(
            "INSERT INTO prompts (id, source, source_session_id, agent_id, turn_index,"
            " ts_observed, ts_source, recency_rank, prompt, response_excerpt, title,"
            " app_context, deep_link, content_hash, provenance, message_id, author,"
            " first_seen_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, item.source, item.session_id, item.agent_id, idx, now, ts_source,
             item.recency_rank, prompt, response, title,
             item.app_context, item.deep_link, chash, provenance,
             blk.get("message_id"), blk.get("author"), now, now))
        new_rows += 1

    if conflicts:
        pass    # surfaced via the return value; see _extract_loop

    first = blocks[0] if blocks else {}
    conn.execute(
        "UPDATE harvest_state SET content_hash=?, last_harvested_at=?,"
        " title=COALESCE(NULLIF(?,''), title),"
        " created_utc=COALESCE(?, created_utc),"
        " updated_utc=COALESCE(?, updated_utc)"
        " WHERE source=? AND source_session_id=?",
        (chash, now, first.get("title") or item.title,
         first.get("created_utc"), first.get("updated_utc"),
         item.source, item.session_id))
    return new_rows, enriched, conflicts


# --------------------------------------------------------------------------- run

def _extract(page, item: Item) -> list[dict]:
    return extract_chat(page, item) if item.source == "m365-copilot" else extract_cowork(page, item)


def _extract_loop(page, conn, items: list[Item], *, deadline: float, provenance: str,
                  totals: dict, log, overwrite_conflicts: bool = False) -> None:
    for n, item in enumerate(items, 1):
        if time.time() > deadline:
            log(f"  time budget reached after {n - 1}/{len(items)}")
            break
        try:
            blocks = _extract(page, item)
        except SignInRequired:
            raise
        except Exception as exc:                    # noqa: BLE001 - logged, continue
            log(f"  ! {item.session_id[:8]} {type(exc).__name__}: {exc}")
            continue
        totals["visited"] += 1
        if not blocks:
            totals["empty"] += 1
        new, enriched, conflicts = store(conn, item, blocks, provenance,
                                         overwrite_conflicts=overwrite_conflicts)
        totals["new"] += new
        totals["enriched"] += enriched
        totals["conflicts"] += conflicts
        conn.commit()
        if n % 10 == 0:
            log(f"  …{n}/{len(items)} new={totals['new']} "
                f"enriched={totals['enriched']} conflicts={totals['conflicts']} "
                f"empty={totals['empty']}")


def harvest(mode: str, *, budget_s: float, chat: bool = True, cowork: bool = True,
            enum_budget_s: float | None = None, limit: int = 100_000,
            verbose: bool = True, overwrite_conflicts: bool = False) -> dict:
    db.init_db()
    conn = db.connect()
    run_id = uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO harvest_runs (run_id, started_at, status, mode) VALUES (?,?,?,?)",
                 (run_id, db.now_iso(), "running", mode))
    conn.commit()

    totals = {"enumerated": 0, "visited": 0, "new": 0, "empty": 0,
              "enriched": 0, "conflicts": 0}
    t0 = time.time()
    deadline = t0 + budget_s

    def log(msg):
        # Console writes must never kill a harvest. A run on 18 Aug died with
        # OSError(22) mid-extraction after 90 successful items purely on output,
        # losing the rest of its budget. Non-ASCII is also unsafe on cp1252
        # consoles, so degrade rather than raise.
        if not verbose:
            return
        try:
            print(msg, flush=True)
        except Exception:
            try:
                sys.stdout.write(str(msg).encode("ascii", "replace").decode() + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    sources = ([("m365-copilot", enumerate_chat)] if chat else []) + \
              ([("cowork", enumerate_cowork)] if cowork else [])

    try:
        if mode in ("extract", "backfill"):
            # Interleave sources in blocks. Strict concatenation starves the
            # smaller source: 1,575 chat items queued ahead of 219 Cowork tasks
            # meant Cowork never got reached before the budget expired, and sat
            # at zero extracted for days. Blocks (not 1:1) because switching
            # surfaces costs a full Cowork re-open.
            pick = pending_from_db if mode == "extract" else harvested_from_db
            buckets = [pick(conn, src, limit) for src, _ in sources]
            todo: list[Item] = []
            block = 25
            for i in range(0, max((len(b) for b in buckets), default=0), block):
                for b in buckets:
                    todo.extend(b[i:i + block])
            label = "pending" if mode == "extract" else "incomplete (backfill)"
            log(f"[{mode}] {len(todo)} {label} items "
                f"({', '.join(f'{s}={len(b)}' for (s, _), b in zip(sources, buckets))})")
            if todo:
                with run_lock():
                    with browser_page() as page:
                        _extract_loop(page, conn, todo, deadline=deadline,
                                      provenance="backfill", totals=totals, log=log,
                                      overwrite_conflicts=overwrite_conflicts)
        else:
            with run_lock():
                with browser_page() as page:
                    for source, enum_fn in sources:
                        incremental = mode == "incremental"
                        log(f"[{source}] enumerating ({mode})…")
                        # Batches arrive in sidebar order, so a running count is
                        # the item's true recency position.
                        seen_so_far = {"n": 0}

                        def _record(pairs, s=source, c=seen_so_far):
                            n = record_discovered(conn, s, pairs, rank_offset=c["n"])
                            c["n"] += len(pairs)
                            return n

                        en = enum_fn(
                            page,
                            max_rounds=INCREMENTAL_ROUNDS if incremental else 600,
                            known=known_hrefs(conn, source) if incremental else None,
                            known_streak_stop=KNOWN_STREAK_STOP if incremental else 0,
                            time_budget_s=enum_budget_s,
                            progress=(lambda r, n: log(f"  round {r}: {n}") if r % 10 == 0 else None),
                            on_batch=_record,
                        )
                        totals["enumerated"] += len(en.items)
                        log(f"[{source}] {len(en.items)} items (agents={en.agent_count}, "
                            f"exhausted={en.exhausted}, early={en.stopped_early}, "
                            f"{en.elapsed_s:.0f}s)")

                        # Zero enumeration is never legitimate: it means the nav
                        # did not render, and must not look like a clean run.
                        if not en.items:
                            raise GuardrailTripped(f"{source}: enumerated 0 items")

                        if mode == "discover":
                            continue
                        todo = pending_from_db(conn, source, limit)
                        log(f"[{source}] {len(todo)} to extract")
                        _extract_loop(page, conn, todo, deadline=deadline,
                                      provenance=mode, totals=totals, log=log,
                                      overwrite_conflicts=overwrite_conflicts)

        if totals["visited"] >= 20:
            ratio = totals["empty"] / totals["visited"]
            if ratio > EMPTY_RATIO_ABORT:
                raise GuardrailTripped(
                    f"empty extraction ratio {ratio:.0%} over {totals['visited']} pages")

        status, err = "ok", None
    except LockHeld as exc:
        status, err = "lock_held", str(exc)
    except SignInRequired as exc:
        status, err = "signin_required", str(exc)
    except NavNotReady as exc:
        status, err = "nav_not_ready", str(exc)
    except GuardrailTripped as exc:
        status, err = "guardrail", str(exc)
    except Exception as exc:                        # noqa: BLE001 - recorded in run log
        status, err = "error", f"{type(exc).__name__}: {exc}"

    ratio = (totals["empty"] / totals["visited"]) if totals["visited"] else None
    conn.execute(
        "UPDATE harvest_runs SET ended_at=?, status=?, enumerated=?, visited=?,"
        " items_new=?, empty_ratio=?, error=? WHERE run_id=?",
        (db.now_iso(), status, totals["enumerated"], totals["visited"],
         totals["new"], ratio, err, run_id))
    conn.commit()
    log(f"== {status} :: enumerated={totals['enumerated']} visited={totals['visited']} "
        f"new={totals['new']} empty={totals['empty']} in {time.time() - t0:.0f}s")
    if err:
        log(f"   {err}")
    return {"run_id": run_id, "status": status, "error": err, **totals}


def store_local(conn, lp, provenance: str = "local") -> int:
    """Upsert one locally-collected prompt. Returns 1 if newly inserted."""
    now = db.now_iso()
    pid = db.prompt_id(lp.source, lp.session_id, lp.turn_index)
    if conn.execute("SELECT 1 FROM prompts WHERE id=?", (pid,)).fetchone():
        conn.execute("UPDATE prompts SET last_seen_at=? WHERE id=?", (now, pid))
        return 0
    conn.execute(
        "INSERT INTO prompts (id, source, source_session_id, agent_id, turn_index,"
        " ts_observed, ts_source, recency_rank, prompt, response_excerpt, title,"
        " app_context, deep_link, content_hash, provenance, origin,"
        " first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, lp.source, lp.session_id, None, lp.turn_index, now, lp.ts_source, 0,
         lp.prompt, lp.response_excerpt, lp.title, lp.app_context, lp.deep_link,
         db.content_hash(lp.prompt), provenance, lp.origin, now, now))
    return 1


def harvest_local(*, verbose: bool = True) -> dict:
    """Ingest the local-file surfaces: Scout, Copilot CLI, VS Code.

    No browser, no lock, no budget — these are plain file reads and complete in
    seconds, so this can run as often as we like.
    """
    db.init_db()
    conn = db.connect()
    run_id = uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO harvest_runs (run_id, started_at, status, mode) VALUES (?,?,?,?)",
                 (run_id, db.now_iso(), "running", "local"))
    conn.commit()

    def log(msg):
        if verbose:
            try:
                print(msg, flush=True)
            except Exception:
                pass

    t0 = time.time()
    seen = new = 0
    by_origin: dict[str, int] = {}
    status, err = "ok", None
    try:
        for name, fn in COLLECTORS.items():
            items = fn()
            log(f"[{name}] {len(items)} prompts found")
            for lp in items:
                seen += 1
                added = store_local(conn, lp)
                new += added
                if added:
                    by_origin[lp.origin] = by_origin.get(lp.origin, 0) + 1
            conn.commit()
        if seen == 0:
            raise GuardrailTripped("local collectors returned 0 prompts")
    except GuardrailTripped as exc:
        status, err = "guardrail", str(exc)
    except Exception as exc:                        # noqa: BLE001 - recorded in run log
        status, err = "error", f"{type(exc).__name__}: {exc}"

    usage_results = import_local_usage(conn)
    usage_failures = [r for r in usage_results if r["status"] not in ("ok",)]
    if usage_failures:
        detail = "; ".join(
            f"{r['source']}: {r.get('detail') or r['status']}"
            for r in usage_failures)
        err = f"{err}; usage import: {detail}" if err else f"usage import: {detail}"
        if status == "ok":
            status = "partial"

    conn.execute(
        "UPDATE harvest_runs SET ended_at=?, status=?, enumerated=?, visited=?,"
        " items_new=?, error=? WHERE run_id=?",
        (db.now_iso(), status, seen, seen, new, err, run_id))
    conn.commit()
    log(f"== {status} :: seen={seen} new={new} "
        f"({', '.join(f'{k}={v}' for k, v in sorted(by_origin.items())) or 'nothing new'}) "
        f"in {time.time() - t0:.0f}s")
    if err:
        log(f"   {err}")
    return {"run_id": run_id, "status": status, "seen": seen, "new": new,
            "by_origin": by_origin, "usage": usage_results, "error": err}


def repair(*, verbose: bool = True) -> dict:
    """Re-apply the current stripping and classification rules to stored rows.

    These rules evolve as new host-injected patterns surface (the context block
    was renamed Clawpilot -> Microsoft Scout, attachment metadata leaks in from
    the composer, and so on). Re-harvesting will not fix existing rows, because
    prompt IDs are content-addressed on identity rather than text, so an
    already-seen row is skipped. This walks the corpus and rewrites in place.
    """
    db.init_db()
    conn = db.connect()

    def log(msg):
        if verbose:
            try:
                print(msg, flush=True)
            except Exception:
                pass

    rows = conn.execute("SELECT id, source, prompt, origin, title FROM prompts").fetchall()
    changed_text = changed_origin = 0
    for r in rows:
        new_prompt = strip_injected(r["prompt"])
        new_origin = classify(r["prompt"])
        title = r["title"] or ""
        # Titles come from session summaries, which carry the same injected
        # context blocks. Drop titles that merely echo the prompt.
        new_title = strip_injected(title)
        if r["source"] == "github-copilot":
            # Sidebar-derived titles carry GitHub's status badges; a session
            # that has since gone offline is never revisited by the collector.
            new_title = clean_github_title(new_title)
        if new_title[:120] == new_prompt[:120]:
            new_title = ""
        if new_prompt != r["prompt"] or new_title != title:
            conn.execute("UPDATE prompts SET prompt=?, title=? WHERE id=?",
                         (new_prompt, new_title, r["id"]))
            changed_text += 1
        if new_origin != (r["origin"] or "human"):
            conn.execute("UPDATE prompts SET origin=? WHERE id=?", (new_origin, r["id"]))
            changed_origin += 1
    conn.commit()
    log(f"== repair :: scanned={len(rows)} text_fixed={changed_text} "
        f"origin_fixed={changed_origin}")
    return {"scanned": len(rows), "text_fixed": changed_text,
            "origin_fixed": changed_origin}


def harvest_github(*, budget_s: float = 30 * 60, verbose: bool = True) -> dict:
    """Harvest GitHub Copilot chats and agent sessions from the web UI.

    The desktop app stores nothing locally, so this is the only route to its
    prompts. Volume is small (the sidebar exposes recent items only), so this
    enumerates and extracts in one pass rather than splitting the two.
    """
    db.init_db()
    conn = db.connect()
    run_id = uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO harvest_runs (run_id, started_at, status, mode) VALUES (?,?,?,?)",
                 (run_id, db.now_iso(), "running", "github"))
    conn.commit()

    def log(msg):
        if not verbose:
            return
        try:
            print(msg, flush=True)
        except Exception:
            pass

    t0 = time.time()
    deadline = t0 + budget_s
    totals = {"enumerated": 0, "visited": 0, "new": 0, "empty": 0, "no_transcript": 0}
    status, err = "ok", None

    try:
        with run_lock():
            with browser_page() as page:
                items = enumerate_github(page)
                totals["enumerated"] = len(items)
                log(f"[github] {len(items)} items "
                    f"({sum(1 for i in items if i.kind == 'chat')} chats, "
                    f"{sum(1 for i in items if i.kind == 'task')} sessions)")
                if not items:
                    raise GuardrailTripped("github: enumerated 0 items")

                for n, item in enumerate(items, 1):
                    if time.time() > deadline:
                        log(f"  time budget reached after {n - 1}/{len(items)}")
                        break
                    try:
                        blocks, ts, has_transcript = extract_github(page, item)
                    except SignInRequired:
                        raise
                    except Exception as exc:            # noqa: BLE001 - logged, continue
                        log(f"  ! {item.session_id[:8]} {type(exc).__name__}: {exc}")
                        continue
                    totals["visited"] += 1
                    if not blocks:
                        if has_transcript:
                            totals["empty"] += 1
                            log(f"  - {item.session_id[:8]} empty ({item.kind})")
                        else:
                            totals["no_transcript"] += 1
                            log(f"  . {item.session_id[:8]} unsynced remote session, no transcript")
                        continue
                    totals["new"] += _store_github(conn, item, blocks, ts)
                    conn.commit()
                    log(f"  + {item.session_id[:8]} {len(blocks)} prompts ({item.kind})")

        # Unsynced remote shells hold no transcript by design, so they are
        # excluded from the denominator: otherwise a run full of them would
        # look like a broken selector.
        gradeable = totals["visited"] - totals["no_transcript"]
        if gradeable >= 10:
            ratio = totals["empty"] / gradeable
            if ratio > EMPTY_RATIO_ABORT:
                raise GuardrailTripped(
                    f"empty extraction ratio {ratio:.0%} over {gradeable} pages")
    except LockHeld as exc:
        status, err = "lock_held", str(exc)
    except SignInRequired as exc:
        status, err = "signin_required", str(exc)
    except GuardrailTripped as exc:
        status, err = "guardrail", str(exc)
    except Exception as exc:                            # noqa: BLE001 - recorded in run log
        status, err = "error", f"{type(exc).__name__}: {exc}"

    gradeable = totals["visited"] - totals["no_transcript"]
    ratio = (totals["empty"] / gradeable) if gradeable else None
    conn.execute(
        "UPDATE harvest_runs SET ended_at=?, status=?, enumerated=?, visited=?,"
        " items_new=?, empty_ratio=?, error=? WHERE run_id=?",
        (db.now_iso(), status, totals["enumerated"], totals["visited"],
         totals["new"], ratio, err, run_id))
    conn.commit()
    log(f"== {status} :: enumerated={totals['enumerated']} visited={totals['visited']} "
        f"new={totals['new']} empty={totals['empty']} "
        f"no_transcript={totals['no_transcript']} in {time.time() - t0:.0f}s")
    if err:
        log(f"   {err}")
    return {"run_id": run_id, "status": status, "error": err, **totals}


def _store_github(conn, item, blocks: list[dict], ts_source: str | None) -> int:
    """Store GitHub prompts, skipping any already captured from the local CLI store.

    GitHub Copilot CLI sessions appear in this web UI *and* in the local
    session-store we already read, so the same prompt can arrive twice under
    different ids. We match on exact prompt text within the CLI source and skip
    those, keeping the local copy (it has per-turn timestamps; this page only
    has a session-level one).

    Identity is the (session, content) pair rather than the turn index: the
    transcript lazy-loads, so a later run can see earlier turns and shift every
    index, which would otherwise re-insert the whole session as new rows.
    """
    now = db.now_iso()
    new_rows = 0
    for idx, blk in enumerate(blocks):
        prompt = (blk.get("prompt") or "").strip()
        if not prompt:
            continue
        dup = conn.execute(
            "SELECT 1 FROM prompts WHERE source IN ('copilot-cli','scout') AND prompt=? LIMIT 1",
            (prompt,)).fetchone()
        if dup:
            continue
        chash = db.content_hash(prompt)
        seen = conn.execute(
            "SELECT id FROM prompts WHERE source='github-copilot'"
            " AND source_session_id=? AND content_hash=? LIMIT 1",
            (item.session_id, chash)).fetchone()
        if seen:
            conn.execute(
                "UPDATE prompts SET last_seen_at=?, turn_index=?, title=?, ts_source=?"
                " WHERE id=?",
                (now, idx, item.title[:200], ts_source, seen[0]))
            continue
        pid = db.prompt_id("github-copilot", item.session_id, idx)
        if conn.execute("SELECT 1 FROM prompts WHERE id=?", (pid,)).fetchone():
            pid = f"{pid}-{chash[:8]}"
        conn.execute(
            "INSERT INTO prompts (id, source, source_session_id, agent_id, turn_index,"
            " ts_observed, ts_source, recency_rank, prompt, response_excerpt, title,"
            " app_context, deep_link, content_hash, provenance, origin,"
            " first_seen_at, last_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "github-copilot", item.session_id, None, idx, now, ts_source, 0,
             prompt, (blk.get("response") or "")[:2000], item.title[:200],
             item.kind, item.url, chash, "github",
             classify(prompt), now, now))
        new_rows += 1
    return new_rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Prompt Spider harvester")
    ap.add_argument("--mode",
                    choices=["incremental", "discover", "extract", "backfill",
                             "local", "github", "repair"],
                    default="incremental")
    ap.add_argument("--budget-min", type=float, default=None)
    ap.add_argument("--enum-budget-min", type=float, default=None)
    ap.add_argument("--limit", type=int, default=100_000,
                    help="max items to extract this run")
    ap.add_argument("--no-chat", action="store_true")
    ap.add_argument("--no-cowork", action="store_true")
    ap.add_argument("--overwrite-conflicts", action="store_true",
                    help="let the payload replace stored prompt text where the two "
                         "disagree. Off by default: this is the only path that "
                         "destroys previously harvested text. Run align_check.py "
                         "first to see how many rows are affected.")
    args = ap.parse_args()

    if args.mode == "local":
        res = harvest_local()
        return 0 if res["status"] == "ok" else 1

    if args.mode == "repair":
        repair()
        return 0

    if args.mode == "github":
        res = harvest_github(budget_s=(args.budget_min or 30) * 60)
        return 0 if res["status"] == "ok" else (2 if res["status"] == "lock_held" else 1)

    default_budget = {"incremental": 30, "discover": 600,
                      "extract": 120, "backfill": 120}[args.mode]
    res = harvest(
        args.mode,
        budget_s=(args.budget_min or default_budget) * 60,
        chat=not args.no_chat,
        cowork=not args.no_cowork,
        enum_budget_s=(args.enum_budget_min * 60) if args.enum_budget_min else None,
        limit=args.limit,
        overwrite_conflicts=args.overwrite_conflicts,
    )
    return 0 if res["status"] == "ok" else (2 if res["status"] == "lock_held" else 1)


if __name__ == "__main__":
    sys.exit(main())
